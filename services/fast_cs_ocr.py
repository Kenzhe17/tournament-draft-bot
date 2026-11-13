"""Anchor-based fast OCR для классического экрана CS 4×4 (две таблицы).

Идея: вместо полного RapidOCR (det+cls+rec по ВСЕМ боксам) —
один det-проход, затем recognition-only на выбранных областях:

1. Det на всём кадре → геометрия всех текстовых блоков.
2. Поиск двух панелей (разрыв по X) и строковых кластеров (cy).
3. Header anchors: у каждой панели ищем строку с заголовками
   «ИМЯ» / «K/D/A» / «DMG» — распознаём только боксы-кандидаты
   (несколько строк-кандидатов, не весь кадр).
4. Строки игроков — 4 линии под заголовком с регулярным шагом;
   границы — середины между cy (relative geometry), при
   необходимости уточняются проекцией чернил (OpenCV).
5. Колонки: Nickname левее K/D/A, DMG правее — границы по
   серединам между x-центрами заголовков.
6. Ячейки: recognition-only (text_rec) по det-боксам внутри
   ячейки; пустые ячейки — прямой кроп региона. Пакетная
   обработка всех кропов одним вызовом text_rec.
7. Валидация: 2 панели × 4 игрока, 8 K/D/A (ровно 3 числа),
   8 DMG (целое ≥0), 8 ников. Любая ненадёжность → None →
   вызывающий код падает на полный RapidOCR.

Возвращает dict в формате services.cs_parser.parse_cs_results
(плюс служебный ключ "_rec_boxes" — распознанные боксы для
score-fallback/winner-диагностики). Ничего не выдумывает:
нераспознанное значение = None, а не 0.
"""

from __future__ import annotations

import difflib
import io
import logging
import os
import re
import time
from typing import Any

import numpy as np

from services.cs_parser import (
    KDA_DMG_GLUED_RE,
    KDA_RE,
    NUM_RE,
    SCORE_NUM_RE,
    SKIP_WORDS,
    VS_RE,
    _has_name_chars,
    parse_match_score,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Пороги
# ---------------------------------------------------------------------------

_MIN_PANEL_GAP_FRAC = 0.035   # минимальный разрыв между панелями (доля w)
_MIN_PANEL_GAP_PX = 30.0
_MIN_SIDE_BOXES = 10          # мин. боксов с каждой стороны разрыва
_MAX_HEADER_PROBES = 6        # сколько строк-кандидатов распознаём на панель
_MAX_EXTRA_TOP = 40           # верхняя полоса (счёт/баннер) — лимит rec
_MAX_EXTRA_BOTTOM = 40        # нижняя полоса
_REC_SCORE_MIN = 0.35         # минимальная уверенность rec для принятия

_WS_RE = re.compile(r"\s+")
_NORM_RE = re.compile(r"[^a-zа-яё0-9]", re.IGNORECASE)
_DMG_FULL_RE = re.compile(r"^\d{1,7}$")


def _norm_text(t: str) -> str:
    return _NORM_RE.sub("", (t or "").lower())


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


# Латинские двойники кириллицы: OCR на игровом шрифте выдаёт
# смешанную раскладку («ИMA» = кириллическая И + латинские M,A).
_HOMOGLYPHS = str.maketrans(
    "abcekmoptxy",
    "авеекмортху",
)


def _name_like(text: str) -> bool:
    n = _norm_text(text)
    if not n:
        return False
    folded = n.translate(_HOMOGLYPHS)
    for cand in (n, folded):
        if cand in ("имя", "name", "nma", "hma", "има", "нма"):
            return True
        if "имя" in cand or _ratio(cand, "имя") >= 0.55:
            return True
        if _ratio(cand, "name") >= 0.6 or _ratio(cand, "nma") >= 0.6:
            return True
    return False


def _header_kind(text: str) -> str | None:
    """Тип заголовка колонки по (возможно искажённому) тексту."""
    n = _norm_text(text)
    if not n:
        return None
    # K/D/A
    if "kda" in n or _ratio(n, "kda") >= 0.6 or re.search(
        r"(?i)^\s*k\s*[/\\|\-–—]?\s*d", text or ""
    ):
        return "kda"
    # DMG
    if n.startswith("dmg") or n in ("dm6", "dmg6", "dmg.") or _ratio(
        n, "dmg"
    ) >= 0.65:
        return "dmg"
    # ИМЯ / NAME
    if _name_like(text):
        return "name"
    return None


# ---------------------------------------------------------------------------
# Этап 1: det-only проход
# ---------------------------------------------------------------------------


class _DetOnlyEngine:
    """Только det-сессия RapidOCR v1 — без cls/rec ONNX-сессий.

    RapidOCR() v1 создаёт TextDetector+TextClassifier+TextRecognizer
    безусловно — «det-only» движок на нём всё равно грузил все три
    модели (~лишние сотни МБ RSS и медленный init). Здесь — только
    TextDetector(config["Det"]) + та же геометрия preprocess/
    letterbox/origin-points, что у RapidOCR.
    """

    def __init__(
        self,
        max_side_len: int = 640,
        intra_op_num_threads: int = -1,
    ) -> None:
        import rapidocr_onnxruntime.main as _rom
        from rapidocr_onnxruntime.ch_ppocr_det import TextDetector
        from rapidocr_onnxruntime.utils import (
            UpdateParameters,
            add_round_letterbox,
            increase_min_side,
            read_yaml,
            reduce_max_side,
            update_model_path,
        )

        config = update_model_path(read_yaml(_rom.DEFAULT_CFG_PATH))
        config = UpdateParameters()(
            config,
            max_side_len=max_side_len,
            intra_op_num_threads=intra_op_num_threads,
        )
        g = config["Global"]
        self.max_side_len = g["max_side_len"]
        self.min_side_len = g["min_side_len"]
        self.min_height = g["min_height"]
        self.width_height_ratio = g["width_height_ratio"]
        self.text_det = TextDetector(config["Det"])
        self._reduce_max_side = staticmethod(reduce_max_side)
        self._increase_min_side = staticmethod(increase_min_side)
        self._add_round_letterbox = staticmethod(add_round_letterbox)

    def preprocess(self, img: np.ndarray):
        h, w = img.shape[:2]
        max_value = max(h, w)
        ratio_h = ratio_w = 1.0
        if max_value > self.max_side_len:
            img, ratio_h, ratio_w = self._reduce_max_side(
                img, self.max_side_len
            )
        h, w = img.shape[:2]
        min_value = min(h, w)
        if min_value < self.min_side_len:
            img, ratio_h, ratio_w = self._increase_min_side(
                img, self.min_side_len
            )
        return img, ratio_h, ratio_w

    def maybe_add_letterbox(self, img: np.ndarray, op_record: dict):
        from rapidocr_onnxruntime.main import RapidOCR

        h, w = img.shape[:2]
        if self.width_height_ratio == -1:
            use_limit_ratio = False
        else:
            use_limit_ratio = w / h > self.width_height_ratio
        if h <= self.min_height or use_limit_ratio:
            new_h = max(
                int(w / self.width_height_ratio), self.min_height
            ) * 2
            padding_h = int(abs(new_h - h) / 2)
            block_img = self._add_round_letterbox(
                img, (padding_h, padding_h, 0, 0)
            )
            op_record["padding_1"] = {"top": padding_h, "left": 0}
            return block_img, op_record
        op_record["padding_1"] = {"top": 0, "left": 0}
        return img, op_record

    def auto_text_det(self, img: np.ndarray):
        from rapidocr_onnxruntime.main import RapidOCR

        dt_boxes, det_elapse = self.text_det(img)
        if dt_boxes is None or len(dt_boxes) < 1:
            return None, 0.0
        dt_boxes = RapidOCR.sorted_boxes(dt_boxes)
        return dt_boxes, det_elapse

    def _get_origin_points(self, dt_boxes, op_record, raw_h, raw_w):
        dt_boxes_array = np.array(dt_boxes).astype(np.float32)
        for op in reversed(list(op_record.keys())):
            v = op_record[op]
            if "padding" in op:
                top, left = v.get("top"), v.get("left")
                dt_boxes_array[:, :, 0] -= left
                dt_boxes_array[:, :, 1] -= top
            elif "preprocess" in op:
                ratio_h = v.get("ratio_h")
                ratio_w = v.get("ratio_w")
                dt_boxes_array[:, :, 0] *= ratio_w
                dt_boxes_array[:, :, 1] *= ratio_h
        dt_boxes_array = np.where(dt_boxes_array < 0, 0, dt_boxes_array)
        dt_boxes_array[..., 0] = np.where(
            dt_boxes_array[..., 0] > raw_w, raw_w, dt_boxes_array[..., 0]
        )
        dt_boxes_array[..., 1] = np.where(
            dt_boxes_array[..., 1] > raw_h, raw_h, dt_boxes_array[..., 1]
        )
        return dt_boxes_array


_FAST_DET_ENGINE = None


def _get_fast_det():
    """Отдельный det-only движок с уменьшенным max_side_len.

    PP-OCRv6 det_small на ~640px вместо ~960px: на реальных
    скриншотах теряет <5% боксов, но работает заметно быстрее.
    cls/rec модели не загружаются — recognition идёт через основной
    движок на кропах исходного разрешения.
    """
    global _FAST_DET_ENGINE
    if _FAST_DET_ENGINE is None:
        try:
            _FAST_DET_ENGINE = _DetOnlyEngine(
                max_side_len=640,
                intra_op_num_threads=int(
                    os.environ.get("FAST_DET_THREADS", "-1")
                ),
            )
        except Exception:
            logger.warning("fast det engine init failed", exc_info=True)
            _FAST_DET_ENGINE = False
    if _FAST_DET_ENGINE is False:
        from services.ocr_service import _get_ocr

        return _get_ocr()
    return _FAST_DET_ENGINE


def _detect_boxes(img_bgr: np.ndarray):
    """RapidOCR det без cls/rec → (padded_img, dt_boxes, orig_boxes).

    orig_boxes — np.array (N,4,2) в координатах исходного кадра.
    """
    eng = _get_fast_det()
    raw_h, raw_w = img_bgr.shape[:2]
    op_record: dict[str, Any] = {}
    img, ratio_h, ratio_w = eng.preprocess(img_bgr)
    op_record["preprocess"] = {"ratio_h": ratio_h, "ratio_w": ratio_w}
    img, op_record = eng.maybe_add_letterbox(img, op_record)
    dt_boxes, _elapse = eng.auto_text_det(img)
    if dt_boxes is None or len(dt_boxes) < 1:
        return None
    orig = eng._get_origin_points(dt_boxes, op_record, raw_h, raw_w)
    return img, list(dt_boxes), np.asarray(orig, dtype=np.float32)


def _to_box_dict(poly: np.ndarray, idx: int) -> dict[str, Any]:
    pts = np.asarray(poly, dtype=float).reshape(-1, 2)
    x0, y0 = float(pts[:, 0].min()), float(pts[:, 1].min())
    x1, y1 = float(pts[:, 0].max()), float(pts[:, 1].max())
    return {
        "idx": idx,
        "text": "",
        "x0": x0, "y0": y0, "x1": x1, "y1": y1,
        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
        "w": x1 - x0, "h": y1 - y0,
    }


# ---------------------------------------------------------------------------
# Этап 2: панели и строки (геометрия, без OCR)
# ---------------------------------------------------------------------------


def _find_panel_split(boxes: list[dict], w: int, h: int) -> float | None:
    """X-граница между левой и правой таблицами.

    Ищем наибольший разрыв по cx в центральной трети кадра среди
    «текстовых» боксов нижней ~80% кадра. None — разрыв неуверенный
    (например, детальный squad-экран — одна широкая таблица).
    """
    # Только «табличная» полоса (cy > ~0.385h): счёт «VS», баннер и
    # прочие элементы верхней зоны создают ложные разрывы по cx
    # (например, между именами правой таблицы и её K/D/A).
    # Во всех наблюдаемых layouts header ≈0.41–0.44h, счёт ≤0.37h.
    cand = [
        b for b in boxes
        if 0.385 * h < b["cy"] and 6.0 <= b["h"] <= 0.14 * h
    ]
    if len(cand) < 2 * _MIN_SIDE_BOXES:
        return None
    # Граница — по структуре строк, а не по глобальному cx-разрыву:
    # внутренние колонки (имя|K/D/A|DMG) тоже дают чистые разрывы,
    # и на части скриншотов они шире межтабличного. В каждой
    # строке таблицы (header + 4 игрока) самый широкий разрыв
    # между соседними членами — это канал между таблицами.
    # Оцениваем все строки и берём медиану — устойчиво к строкам,
    # где склейка det-боксов меняет ширину разрыва.
    lo, hi = 0.22 * w, 0.78 * w
    min_gap = max(_MIN_PANEL_GAP_PX, _MIN_PANEL_GAP_FRAC * w)
    lines = _cluster_lines(cand)
    mids: list[float] = []
    for ln in lines:
        mem = sorted(ln["members"], key=lambda b: b["cx"])
        if len(mem) < 6:
            continue
        best_gap, best_mid = 0.0, None
        for i in range(len(mem) - 1):
            gap = mem[i + 1]["cx"] - mem[i]["cx"]
            mid = (mem[i]["cx"] + mem[i + 1]["cx"]) / 2
            if not (lo < mid < hi) or gap < min_gap:
                continue
            if gap > best_gap:
                best_gap, best_mid = gap, mid
        if best_mid is not None:
            mids.append(best_mid)
    if len(mids) < 3:
        return None
    mids.sort()
    return mids[len(mids) // 2]


def _cluster_lines(boxes: list[dict]) -> list[dict]:
    """Кластеризация боксов в горизонтальные строки по cy."""
    if not boxes:
        return []
    hs = sorted(b["h"] for b in boxes)
    med_h = hs[len(hs) // 2] or 12.0
    tol = max(8.0, med_h * 0.6)
    sel = sorted(boxes, key=lambda b: b["cy"])
    lines: list[dict] = []
    for b in sel:
        if lines and abs(b["cy"] - lines[-1]["cy"]) <= tol:
            lines[-1]["members"].append(b)
            # cy — медиана членов (устойчивее к выбросам)
            cys = sorted(m["cy"] for m in lines[-1]["members"])
            lines[-1]["cy"] = cys[len(cys) // 2]
        else:
            lines.append({"cy": b["cy"], "members": [b]})
    for ln in lines:
        ln["x0"] = min(m["x0"] for m in ln["members"])
        ln["x1"] = max(m["x1"] for m in ln["members"])
    return lines


def _line_pitch(lines: list[dict], start: int, count: int = 5) -> float:
    """Медианный шаг между соседними строками от индекса start."""
    diffs = []
    for i in range(start, min(start + count, len(lines) - 1)):
        d = lines[i + 1]["cy"] - lines[i]["cy"]
        if d > 1:
            diffs.append(d)
    if not diffs:
        return 0.0
    diffs.sort()
    return diffs[len(diffs) // 2]


# ---------------------------------------------------------------------------
# Этап 3–5: anchors, строки, колонки, rec ячеек
# ---------------------------------------------------------------------------


class _RecCache:
    """Кэш recognition-only: det-бокс idx → (text, score)."""

    def __init__(self, eng, img_padded, dt_boxes):
        self.eng = eng
        self.img_padded = img_padded
        self.dt_boxes = dt_boxes
        self.cache: dict[int, tuple[str, float]] = {}
        self._pending: list[int] = []
        # Профилирование rec-стадии: вызовы/кропы/время prep и rec.
        self.stats = {
            "calls": 0, "crops": 0, "prep_ms": 0.0, "rec_ms": 0.0,
            "max_crop_w": 0,
        }

    def want(self, idx: int) -> None:
        if idx not in self.cache and idx not in self._pending:
            self._pending.append(idx)

    def flush(self) -> None:
        """Один batch-rec всех ожидающих det-боксов."""
        if not self._pending:
            return
        idxs = self._pending
        self._pending = []
        t_prep = time.perf_counter()
        crops = self.eng.get_crop_img_list(
            self.img_padded, [self.dt_boxes[i] for i in idxs]
        )
        crops = _cap_crop_ratios(crops or [])
        prep_ms = (time.perf_counter() - t_prep) * 1000
        t_rec = time.perf_counter()
        res, _ = self.eng.text_rec(crops)
        rec_ms = (time.perf_counter() - t_rec) * 1000
        self.stats["calls"] += 1
        self.stats["crops"] += len(crops or [])
        self.stats["prep_ms"] += prep_ms
        self.stats["rec_ms"] += rec_ms
        for c in crops or []:
            try:
                self.stats["max_crop_w"] = max(
                    self.stats["max_crop_w"], int(c.shape[1])
                )
            except Exception:
                pass
        for i, r in zip(idxs, res or []):
            try:
                text, score = r[0], float(r[1])
            except (TypeError, IndexError):
                text, score = "", 0.0
            self.cache[i] = (str(text).strip(), score)

    def get(self, idx: int) -> tuple[str, float]:
        return self.cache.get(idx, ("", 0.0))


# rec_img_shape [3,48,320] → нативный max aspect = 320/48 ≈ 6.67.
# Шире — text_rec всё равно падает до height=48, но тензор батча
# растягивается до 48*ratio: один широкий баннер-кроп раздувает
# весь batch. Кропы с ratio > лимита дожимаем по ширине —
# вертикальное разрешение (главное для CTC) не меняется.
_REC_MAX_WH_RATIO = 320.0 / 48.0


def _cap_crop_ratios(crops: list) -> list:
    import cv2

    out = []
    for c in crops:
        h, w = c.shape[:2]
        ratio = w / max(1, h)
        if ratio > _REC_MAX_WH_RATIO:
            nw = max(8, int(h * _REC_MAX_WH_RATIO))
            c = cv2.resize(c, (nw, h), interpolation=cv2.INTER_AREA)
        out.append(c)
    return out


def _rec_crop_rect(img_bgr: np.ndarray, rect, min_h: int = 36):
    """Кроп произвольного прямоугольника → BGR ndarray для text_rec."""
    import cv2

    h, w = img_bgr.shape[:2]
    x0 = max(0, int(rect[0]))
    y0 = max(0, int(rect[1]))
    x1 = min(w, int(rect[2]))
    y1 = min(h, int(rect[3]))
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    crop = img_bgr[y0:y1, x0:x1]
    if crop.shape[0] < min_h:
        scale = min(4.0, min_h / crop.shape[0])
        crop = cv2.resize(
            crop, None, fx=scale, fy=scale,
            interpolation=cv2.INTER_CUBIC,
        )
    return np.ascontiguousarray(crop)


class _RectRec:
    """Recognition-only для пустых ячеек (кропы без det)."""

    def __init__(self, eng, img_bgr):
        self.eng = eng
        self.img_bgr = img_bgr
        self.jobs: list[tuple] = []  # (key, rect)
        self.stats = {
            "calls": 0, "crops": 0, "prep_ms": 0.0, "rec_ms": 0.0,
            "max_crop_w": 0,
        }

    def add(self, key, rect):
        self.jobs.append((key, rect))

    def flush(self):
        crops = []
        keys = []
        t_prep = time.perf_counter()
        for key, rect in self.jobs:
            c = _rec_crop_rect(self.img_bgr, rect)
            if c is not None:
                crops.append(c)
                keys.append(key)
        prep_ms = (time.perf_counter() - t_prep) * 1000
        self.jobs = []
        crops = _cap_crop_ratios(crops)
        if not crops:
            return {}
        t_rec = time.perf_counter()
        res, _ = self.eng.text_rec(crops)
        rec_ms = (time.perf_counter() - t_rec) * 1000
        self.stats["calls"] += 1
        self.stats["crops"] += len(crops)
        self.stats["prep_ms"] += prep_ms
        self.stats["rec_ms"] += rec_ms
        for c in crops:
            self.stats["max_crop_w"] = max(
                self.stats["max_crop_w"], int(c.shape[1])
            )
        out = {}
        for key, r in zip(keys, res or []):
            try:
                out[key] = (str(r[0]).strip(), float(r[1]))
            except (TypeError, IndexError):
                out[key] = ("", 0.0)
        return out


def _match_header_line(line: dict, rec: _RecCache) -> dict | None:
    """Строка = заголовки колонок? Возвращает {name,kda,dmg: box}."""
    found: dict[str, dict] = {}
    for m in line["members"]:
        text, score = rec.get(m["idx"])
        if score < _REC_SCORE_MIN:
            continue
        kind = _header_kind(text)
        if kind and kind not in found:
            found[kind] = m
    if not all(k in found for k in ("name", "kda", "dmg")):
        return None
    # Порядок колонок: ИМЯ левее K/D/A, DMG правее K/D/A
    if not (
        found["name"]["cx"] < found["kda"]["cx"] < found["dmg"]["cx"]
    ):
        return None
    return found


_PANEL_FAILS: list[str] = []
_PANEL_DEBUG = os.environ.get("FAST_OCR_DEBUG") == "1"


def _panel_fail(side: str, why: str) -> None:
    # Диагностика только при FAST_OCR_DEBUG=1 — иначе no-op.
    if _PANEL_DEBUG:
        _PANEL_FAILS.append(f"{side}:{why}")


def _panel_geometry(
    panel_boxes: list[dict],
    boundary_x: float,
    img_w: int,
    img_h: int,
    side: str,
    rec: _RecCache,
) -> dict | None:
    """Anchor detection + rows + columns для одной панели.

    Возвращает {"header": {...}, "rows": [cy×4], "pitch": p,
    "cols": {name,kda,dmg: (x0,x1)}, "name_region": (x0,y0,x1,y1),
    "table_top": cy, "table_bottom": cy} или None.
    """
    lines = _cluster_lines(panel_boxes)
    # Минимум: header + 4 строки игроков = 5 линий
    if len(lines) < 5:
        _panel_fail(side, "few_lines"); return None

    header = None
    header_li = -1
    probed = 0
    for li, ln in enumerate(lines):
        if probed >= _MAX_HEADER_PROBES:
            break
        # Кандидат: ≥3 боксов и ниже ≥4 строк (4 игрока)
        if len(ln["members"]) < 3:
            continue
        # «Контентные» строки под кандидатом: ≥2 боксов — у stat-строки
        # всегда K/D/A + DMG (+ник), а однобоксовые линии — это вторая
        # строка доп. текста под ником («GHTERRA», «Republic KZ»),
        # которая ломала проверку шага и давала no_anchor.
        sub = [
            l for l in lines[li + 1 :] if len(l["members"]) >= 2
        ][:8]
        if len(sub) < 4:
            continue
        # Строки под кандидатом должны иметь регулярный шаг.
        # Оцениваем только первые 4 контентные строки — дальше
        # могут быть посторонние линии (топ-5, вторая строка низа).
        pitch = _line_pitch(sub, 0, 3)
        if pitch <= 0:
            continue
        ok = True
        for j in range(1, min(4, len(sub))):
            d = sub[j]["cy"] - sub[j - 1]["cy"]
            if d <= 0.4 * pitch or d > 2.2 * pitch:
                ok = False
                break
        if not ok:
            continue
        probed += 1
        for m in ln["members"]:
            rec.want(m["idx"])
        rec.flush()
        header = _match_header_line(ln, rec)
        if header:
            header_li = li
            break
    if header is None:
        _panel_fail(side, "no_header"); return None

    header_cy = lines[header_li]["cy"]

    # --- Строки игроков: линии ниже заголовка с боксом в K/D/A-колонке
    b1 = (header["name"]["x1"] + header["kda"]["x0"]) / 2
    b2 = (header["kda"]["x1"] + header["dmg"]["x0"]) / 2
    kda_lo, kda_hi = b1, b2

    row_lines = []
    for ln in lines[header_li + 1 :]:
        if ln["cy"] <= header_cy + 0.3 * (_line_pitch(lines, header_li + 1, 4) or 30):
            continue
        has_kda = any(
            kda_lo - 8 <= m["cx"] <= kda_hi + 8 for m in ln["members"]
        )
        if has_kda:
            row_lines.append(ln)
        if len(row_lines) >= 6:
            break
    if len(row_lines) < 4:
        _panel_fail(side, "few_rows"); return None

    # Ровно 4 строки: pitch по первым 4, снап к регулярной сетке
    cys = [ln["cy"] for ln in row_lines[:6]]
    # Под таблицей могут быть посторонние боксы в K/D/A-колонке
    # (футер, вторая строка низа) — выбираем скользящим окном 4
    # строки с минимальной ошибкой регулярной сетки.
    best = None  # (score, top, pitch)
    for w0 in range(0, max(1, len(cys) - 3)):
        wc = cys[w0 : w0 + 4]
        if len(wc) < 4:
            break
        diffs = [
            wc[i + 1] - wc[i] for i in range(3) if wc[i + 1] > wc[i]
        ]
        if len(diffs) < 2:
            continue
        p = sorted(diffs)[len(diffs) // 2]
        if p <= 0:
            continue
        bt, be = None, float("inf")
        for j, cy in enumerate(wc):
            top = cy - j * p
            err = sum(
                min(abs(c2 - (top + i * p)) for i in range(4))
                for c2 in wc
            )
            if err < be:
                be, bt = err, top
        score = be / 4.0
        if best is None or score < best[0]:
            best = (score, bt, p)
    if best is None or best[0] > 0.45 * best[2]:
        _panel_fail(side, "bad_grid"); return None
    _score, best_top, pitch = best
    rows_cy = [best_top + i * pitch for i in range(4)]
    # Строка существует только если рядом есть реальная линия (±0.45 pitch)
    for rc in rows_cy:
        if not any(abs(ln["cy"] - rc) <= 0.45 * pitch for ln in row_lines):
            _panel_fail(side, "grid_snap"); return None

    # Границы строк — середины между cy; концы ±0.5 pitch
    bounds = []
    for i in range(4):
        if i == 0:
            y_lo = rows_cy[0] - 0.5 * pitch
        else:
            y_lo = (rows_cy[i - 1] + rows_cy[i]) / 2
        if i == 3:
            y_hi = rows_cy[3] + 0.5 * pitch
        else:
            y_hi = (rows_cy[i] + rows_cy[i + 1]) / 2
        bounds.append((y_lo, y_hi))

    # --- Колонки: границы — середины между x-центрами заголовков
    name_x0 = min(m["x0"] for ln in row_lines[:4] for m in ln["members"]) - 6
    dmg_x1 = max(m["x1"] for ln in row_lines[:4] for m in ln["members"]) + 6
    if side == "left":
        name_x0 = max(0.0, name_x0)
        dmg_x1 = min(float(boundary_x), dmg_x1)
    else:
        name_x0 = max(float(boundary_x), name_x0)
        dmg_x1 = min(float(img_w), dmg_x1)
    cols = {
        "name": (name_x0, b1),
        "kda": (b1, b2),
        "dmg": (b2, dmg_x1),
    }

    # Регион названия команды — над заголовками (для cyrillic re-OCR)
    name_region = (
        name_x0,
        max(0.0, header_cy - 2.6 * pitch),
        dmg_x1,
        header_cy - 0.55 * pitch,
    )

    return {
        "header": header,
        "header_cy": header_cy,
        "rows_cy": rows_cy,
        "pitch": pitch,
        "bounds": bounds,
        "cols": cols,
        "name_region": name_region,
        "panel_boxes": panel_boxes,
        "panel_x": (name_x0, dmg_x1),
    }


# ---------------------------------------------------------------------------
# Парсинг распознанного
# ---------------------------------------------------------------------------


def _join_texts(items: list[tuple[str, float]]) -> str:
    return " ".join(t for t, _s in sorted(items) if t)


def _parse_kda_cell(text: str):
    """K/D/A ячейка → (k,d,a) или None. Ровно три числа."""
    t = (text or "").strip()
    if not t:
        return None
    m = KDA_DMG_GLUED_RE.search(t)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = KDA_RE.search(t)
    if not m:
        return None
    # Хвост после K/D/A: ещё один K/D/A — склейка двух ячеек;
    # длинное число — урон, попавший в колонку K/D/A (граница
    # колонок сместилась) — такое не доверяем.
    tail = t[m.end():].strip()
    if KDA_RE.search(tail) or re.search(r"\d{3,}", tail):
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _parse_dmg_cell(text: str):
    """DMG ячейка → int ≥0 или None. Не заменяем нулём."""
    t = _WS_RE.sub("", text or "")
    if not _DMG_FULL_RE.match(t):
        return None
    v = int(t)
    return v if v >= 0 else None


def _parse_name_cell(items: list[tuple[str, float]]) -> str:
    parts = []
    seen = set()
    for _x, (text, _score) in sorted(items):
        t = (text or "").strip()
        if not _name_text_ok(t):
            continue
        if t not in seen:
            seen.add(t)
            parts.append(t)
    name = " ".join(parts)
    return name[:32] if name else ""


def _name_text_ok(t: str) -> bool:
    t = (t or "").strip()
    return bool(
        t and _has_name_chars(t) and len(t) >= 2
        and t.lower() not in SKIP_WORDS and not NUM_RE.match(t)
    )


def _cluster_name_lines(
    items: list[dict], pitch: float, row_cy: float
) -> tuple[str, str]:
    """Разделить name-ячейку на строки по cy → (ник, secondary).

    items: [{cx, cy, y0, y1, text, score}]. Ник — кластер, ближайший
    к строке ника (~row_cy − 0.2·pitch); secondary — кластеры НИЖЕ
    ника (клан/гильдия/водяной знак). Кластеры выше ника — вероятная
    утечка соседней строки, отбрасываются.
    """
    def _cluster_ok(t: str) -> bool:
        # Чистые числа («1337», «444») — допустимый клан-тег второй
        # строки, но никогда не ник.
        t = (t or "").strip()
        return bool(
            (len(t) >= 3 if NUM_RE.match(t) else len(t) >= 2)
            and t.lower() not in SKIP_WORDS
            and (_has_name_chars(t) or NUM_RE.match(t))
        )

    valid = [it for it in items if _cluster_ok(it.get("text"))]
    if not valid:
        return "", ""
    valid.sort(key=lambda it: it["cy"])
    gap = max(14.0, 0.24 * pitch)
    clusters: list[list[dict]] = []
    for it in valid:
        if clusters and it["cy"] - max(c["cy"] for c in clusters[-1]) <= gap:
            clusters[-1].append(it)
        else:
            clusters.append([it])
    lines = []
    for cl in clusters:
        cl.sort(key=lambda it: it["cx"])
        seen: set[str] = set()
        parts = []
        for c in cl:
            t = (c["text"] or "").strip()
            if t and t not in seen:
                seen.add(t)
                parts.append(t)
        text = " ".join(parts)
        if not text:
            continue
        lines.append({
            "cy": sum(c["cy"] for c in cl) / len(cl),
            "text": text[:48],
            # Ник = все части строки, если в ней есть буквенный
            # фрагмент («nurss 444», «uluking 1337» — число может
            # быть частью ника). Числа не могут ОБРАЗОВАТЬ ник.
            "nick_text": (
                " ".join(parts)[:32]
                if any(_name_text_ok(p) for p in parts) else ""
            ),
        })
    if not lines:
        return "", ""
    # Ник — ближайший к ожидаемой позиции строки ника; кластер без
    # валидного ник-текста (только числа/мусор) ником быть не может.
    nick_y = row_cy - 0.2 * pitch
    cands = [i for i, l in enumerate(lines) if l["nick_text"]]
    if not cands:
        return "", ""
    nick_i = min(cands, key=lambda i: abs(lines[i]["cy"] - nick_y))
    name = lines[nick_i]["nick_text"]
    sec_parts = [
        l["text"] for l in lines[nick_i + 1:]
        if l["cy"] > lines[nick_i]["cy"] + 0.5 * gap
    ]
    secondary = " ".join(sec_parts)[:48] if sec_parts else ""
    return name, secondary


def _split_merged_name_boxes(
    eng, img: np.ndarray, jobs: list[tuple]
) -> dict[int, list[dict]]:
    """Разрезать высокие name-боксы (2 строки в одном det) и rec.

    jobs: [(box_idx, box)] — box в координатах исходного кадра.
    Возвращает box_idx → два pseudo-item с cy верх/низ половины.
    Один batch text_rec на все разрезанные половины.
    """
    import cv2

    halves: list[tuple[int, float, np.ndarray]] = []
    h_img, w_img = img.shape[:2]
    for bidx, b in jobs:
        x0 = max(0, int(b["x0"]))
        y0 = max(0, int(b["y0"]))
        x1 = min(w_img, int(b["x1"]))
        y1 = min(h_img, int(b["y1"]))
        if x1 - x0 < 8 or y1 - y0 < 12:
            continue
        crop = img[y0:y1, x0:x1]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        ink = (gray > 120).sum(axis=1)
        # Полосы чернил по Y: >=2 отдельных полосы = реально две
        # строки текста. Одна полоса (высокий бокс с паддингом) —
        # резать нельзя, целиковый rec уже в кэше.
        peak = int(ink.max()) if ink.size else 0
        if peak < 4:
            continue
        thr = max(3, peak // 8)
        bands: list[list[int]] = []
        for yi, v in enumerate(ink):
            if v >= thr:
                if bands and yi - bands[-1][1] <= 4:
                    bands[-1][1] = yi
                else:
                    bands.append([yi, yi])
        if len(bands) < 2:
            continue
        # Разрез — середина самого широкого межполосного зазора.
        gaps = [
            (bands[i + 1][0] - bands[i][1],
             (bands[i][1] + bands[i + 1][0]) // 2)
            for i in range(len(bands) - 1)
        ]
        split = max(gaps)[1]
        top = crop[:split]
        bot = crop[split:]
        if top.shape[0] < 8 or bot.shape[0] < 8:
            continue
        halves.append((bidx, (b["cy"] - (b["y1"] - b["y0"]) * 0.25), top))
        halves.append((bidx, (b["cy"] + (b["y1"] - b["y0"]) * 0.25), bot))
    if not halves:
        return {}
    crops = _cap_crop_ratios([np.ascontiguousarray(h[2]) for h in halves])
    try:
        res, _ = eng.text_rec(crops)
    except Exception:
        return {}
    out: dict[int, list[dict]] = {}
    for (bidx, cy, _c), r in zip(halves, res or []):
        try:
            text, score = str(r[0]).strip(), float(r[1])
        except (TypeError, IndexError):
            text, score = "", 0.0
        if not text:
            continue
        out.setdefault(bidx, []).append({
            "cx": 0.0, "cy": cy, "y0": 0.0, "y1": 0.0,
            "text": text, "score": score,
        })
    return out


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------


def _extract_parsed(
    data: bytes,
    t_submit: float | None = None,
    reason_out: list | None = None,
) -> dict[str, Any] | None:
    """Sync-версия: bytes → parsed dict (формат parse_cs_results).

    t_submit — метка submit в OCR_EXECUTOR (для queue-wait метрики).
    reason_out — необязательный list: причина fallback дописывается
    туда (per-call, без гонки глобального состояния).
    """
    import cv2
    from PIL import Image

    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    if reason_out is not None:
        timings["_reason_sink"] = reason_out
    if t_submit is not None:
        timings["queue_ms"] = max(0.0, (t0 - t_submit) * 1000.0)
    if _PANEL_DEBUG:
        _PANEL_FAILS.clear()

    try:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        img = None
    if img is None:
        try:
            pil = Image.open(io.BytesIO(data)).convert("RGB")
            img = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
        except Exception:
            return None
    timings["decode_ms"] = (time.perf_counter() - t0) * 1000
    img_h, img_w = img.shape[:2]
    if img_w < 200 or img_h < 200:
        return None

    # --- Этап 1: det ---
    t_det = time.perf_counter()
    det = _detect_boxes(img)
    timings["det_ms"] = (time.perf_counter() - t_det) * 1000
    if det is None:
        _record_metrics(timings, t0, ok=False, reason="no_det")
        return None
    _img_padded, _dt_boxes, orig = det

    boxes = [
        _to_box_dict(orig[i], i) for i in range(len(orig))
    ]

    # --- Этап 2: разрыв панелей ---
    boundary = _find_panel_split(boxes, img_w, img_h)
    if boundary is None:
        _record_metrics(timings, t0, ok=False, reason="no_split")
        return None

    left_boxes = [b for b in boxes if b["cx"] < boundary]
    right_boxes = [b for b in boxes if b["cx"] >= boundary]
    if len(left_boxes) < _MIN_SIDE_BOXES or len(right_boxes) < _MIN_SIDE_BOXES:
        _record_metrics(timings, t0, ok=False, reason="thin_panel")
        return None

    from services.ocr_service import _get_ocr

    eng = _get_ocr()
    # Rec-кропы — из исходного кадра по orig-координатам (полное
    # разрешение), а не из уменьшенного det-входа fast-движка.
    rec = _RecCache(eng, img, orig)
    rect_rec = _RectRec(eng, img)

    # --- Этапы 3–5: anchors/rows/cols для обеих панелей ---
    # (включает header-rec: распознавание строк-кандидатов заголовков)
    t_anch = time.perf_counter()
    geoms = []
    for side, pboxes in (("left", left_boxes), ("right", right_boxes)):
        g = _panel_geometry(pboxes, boundary, img_w, img_h, side, rec)
        if g is None:
            _record_metrics(timings, t0, ok=False, reason=f"no_anchor_{side}")
            return None
        geoms.append(g)
    timings["anchors_ms"] = (time.perf_counter() - t_anch) * 1000

    # --- Этап 6: выбор det-боксов для rec ячеек ---
    # cell_jobs: (panel_i, row_i, col) -> [member boxes]
    rec_s0 = dict(rec.stats)
    rect_s0 = dict(rect_rec.stats)
    t_cell = time.perf_counter()
    cell_members: dict[tuple, list[dict]] = {}
    for pi, g in enumerate(geoms):
        pboxes = g["panel_boxes"]
        for ri in range(4):
            y_lo, y_hi = g["bounds"][ri]
            row_cy = g["rows_cy"][ri]
            pitch = g["pitch"]
            for col in ("name", "kda", "dmg"):
                cx0, cx1 = g["cols"][col]
                members = []
                for b in pboxes:
                    if not (cx0 - 2 <= b["cx"] <= cx1 + 2):
                        continue
                    if col == "name":
                        # Имя + возможная вторая строка (клан/гильдия)
                        # лежат вокруг линии K/D/A: ник ~−0.2·pitch,
                        # тег ~+0.15–0.4·pitch. Полоса ±0.5·pitch
                        # захватывает обе, но не соседние строки
                        # (±1.0·pitch). Разделение — по cy-кластерам.
                        if abs(b["cy"] - row_cy) > 0.50 * pitch:
                            continue
                    elif not (y_lo - 2 <= b["cy"] <= y_hi + 2):
                        continue
                    members.append(b)
                members.sort(key=lambda b: b["x0"])
                cell_members[(pi, ri, col)] = members
                if not members and col != "name":
                    # Пустая числовая ячейка — прямой кроп региона
                    rect_rec.add(
                        (pi, ri, col),
                        (cx0, y_lo - 2, cx1, y_hi + 2),
                    )
                elif not members and col == "name":
                    # Имя без det-бокса — кроп только если полоса
                    # действительно пустая (нет риска аватара+текста)
                    rect_rec.add(
                        (pi, ri, "name"),
                        (cx0, row_cy - 0.40 * pitch, cx1, row_cy + 0.40 * pitch),
                    )

    # Все det-боксы ячеек — один batch
    for members in cell_members.values():
        for b in members:
            rec.want(b["idx"])
    rec.flush()
    rect_results = rect_rec.flush()
    timings["cells_ms"] = (time.perf_counter() - t_cell) * 1000
    for prefix, st, s0 in (
        ("cells_det", rec.stats, rec_s0),
        ("cells_rect", rect_rec.stats, rect_s0),
    ):
        for k in ("calls", "crops", "prep_ms", "rec_ms", "max_crop_w"):
            v = st[k] - (s0.get(k) or 0)
            timings[f"{prefix}_{k}"] = v

    # --- Этап 7: верхняя/нижняя полосы (счёт, баннер, top5) ---
    # Счёт «N VS M» сидит всего в ~1 pitch над заголовком — полоса
    # должна доставать до него, но не захватывать строки игроков.
    # Берём ближайшие к заголовку боксы (desc cy), чтобы верхний
    # мусор (CJK-шум по краям) не вытеснял счёт из лимита rec.
    bands_s0 = dict(rec.stats)
    t_band = time.perf_counter()
    extra_idxs: list[int] = []
    top_limit = min(g["header_cy"] for g in geoms) - 0.4 * max(
        g["pitch"] for g in geoms
    )
    bot_limit = max(g["rows_cy"][-1] for g in geoms) + 1.4 * max(
        g["pitch"] for g in geoms
    )
    top_c = sorted(
        (b for b in boxes if b["cy"] < top_limit), key=lambda b: -b["cy"]
    )[:_MAX_EXTRA_TOP]
    bot_c = sorted(
        (b for b in boxes if b["cy"] > bot_limit), key=lambda b: -b["cy"]
    )[:_MAX_EXTRA_BOTTOM]
    for b in top_c + bot_c:
        rec.want(b["idx"])
        extra_idxs.append(b["idx"])
    rec.flush()
    timings["bands_ms"] = (time.perf_counter() - t_band) * 1000
    for k in ("calls", "crops", "prep_ms", "rec_ms", "max_crop_w"):
        timings[f"bands_det_{k}"] = rec.stats[k] - (bands_s0.get(k) or 0)

    # Счёт «N VS M»: одиночные цифры сена на быстрой det-модели
    # могут не давать det-боксов — добираем rect-rec полосами
    # ±0.16w вокруг «VS» и добавляем как синтетические боксы.
    t_score = time.perf_counter()
    score_extra: list[dict] = []
    vs_b = None
    for b in boxes:
        t, s = rec.get(b["idx"])
        # «VS» — короткий латинский маркер, rec-score у него часто
        # низкий; само совпадение текста достаточно специфично.
        if VS_RE.match(t.strip()) and (s >= 0.15 or t.strip().upper() == "VS"):
            vs_b = b
            break
    score_rect: dict = {}
    if vs_b is not None:
        # Если цифра счёта уже есть как det-бокс рядом с «VS», strip-rec
        # для этой стороны не нужен — он может подхватить мусор.
        have_left = have_right = False
        for b in boxes:
            if abs(b["cy"] - vs_b["cy"]) > 20:
                continue
            t, s = rec.get(b["idx"])
            if s < _REC_SCORE_MIN or not SCORE_NUM_RE.match(t.strip()):
                continue
            if b["cx"] < vs_b["cx"] - 15:
                have_left = True
            elif b["cx"] > vs_b["cx"] + 15:
                have_right = True
        sides = {}
        if not have_left:
            sides[("score", "sl")] = (
                max(0.0, vs_b["cx"] - 160.0), vs_b["cx"] - 25.0
            )
        if not have_right:
            sides[("score", "sr")] = (
                vs_b["cx"] + 25.0, min(float(img_w), vs_b["cx"] + 160.0)
            )
        bands: list[float] = []
        if sides:
            # Цифра счёта — одного размера с «VS»; узкое окно ±25–160px
            # отсекает соседний UI. band's от pitch строки (высота «VS»-
            # бокса может быть завышена склейкой) — rec на полосе
            # нестабилен, берём лучший валидный результат.
            pitch = max(g["pitch"] for g in geoms)
            bands = sorted({
                min(max(0.45 * pitch, 24.0), 64.0),
                min(max(0.60 * pitch, 30.0), 64.0),
                min(max(0.75 * pitch, 36.0), 64.0),
            })
            for (key, (x0, x1)), band in (
                (kv, band) for kv in sides.items() for band in bands
            ):
                if x1 - x0 >= 8.0:
                    rect_rec.add((key[0], key[1], int(band)),
                                 (x0, vs_b["cy"] - band, x1, vs_b["cy"] + band))
            score_rect = rect_rec.flush()
        for side, cx in (
            ("sl", (max(0.0, vs_b["cx"] - 160.0) + vs_b["cx"] - 25.0) / 2),
            ("sr", (vs_b["cx"] + 25.0 + min(float(img_w), vs_b["cx"] + 160.0)) / 2),
        ):
            best: tuple[str, float] | None = None
            for band in bands:
                t, s = score_rect.get(("score", side, int(band)), ("", 0.0))
                t = (t or "").strip()
                # Только целое ≤30 с rec-score ≥0.5 — отсекает склейки
                # вида «55»/«41» и мусорные чтения на краях полосы.
                if (SCORE_NUM_RE.match(t) and int(t) <= 30
                        and s >= 0.5 and (best is None or s > best[1])):
                    best = (t, s)
            if best is not None:
                band = bands[0]
                score_extra.append(
                    {
                        "text": best[0], "x0": cx - 5, "y0": vs_b["cy"] - band,
                        "x1": cx + 5, "y1": vs_b["cy"] + band,
                        "cx": cx, "cy": vs_b["cy"],
                    }
                )

    timings["score_ms"] = (time.perf_counter() - t_score) * 1000

    # --- Этап 8: сборка players ---
    # Предпроход: name-боксы выше ~0.6·pitch содержат две строки
    # (ник + клан/гильдия в одном det) — разрезаем и rec отдельно,
    # иначе rec склеивает их в одну строку.
    max_pitch = max(g["pitch"] for g in geoms)
    merged_jobs = []
    for (pi, ri, col), members in cell_members.items():
        if col != "name":
            continue
        for b in members:
            if (b["y1"] - b["y0"]) > 0.68 * max_pitch:
                merged_jobs.append((b["idx"], b))
    merged_texts = _split_merged_name_boxes(eng, img, merged_jobs)
    box_by_idx = {b["idx"]: b for b in boxes}

    def _rec_text_for(b: dict) -> tuple[str, float]:
        return rec.get(b["idx"])

    players: dict[str, list] = {"left": [], "right": []}
    n_kda = n_dmg = n_names = 0
    for pi, side in enumerate(("left", "right")):
        g = geoms[pi]
        for ri in range(4):
            row_cy = g["rows_cy"][ri]
            pitch = g["pitch"]
            y_lo, y_hi = g["bounds"][ri]
            rh = max(8.0, pitch * 0.45)

            # K/D/A: лучший валидный кандидат среди боксов ячейки.
            # Мусорные детекции («+», «G») отбрасываются парсером
            # формата, а не склейкой в одну строку.
            kda = None
            kda_best = -1.0
            for b in cell_members[(pi, ri, "kda")]:
                t, s = _rec_text_for(b)
                if not t or s < _REC_SCORE_MIN or s <= kda_best:
                    continue
                k = _parse_kda_cell(t)
                if k is not None:
                    kda, kda_best = k, s
            if kda is None:
                # K/D/A может быть разрезан det'ом на несколько
                # боксов («6/» + «5/1») — пробуем склейку по x.
                parts = []
                for b in sorted(
                    cell_members[(pi, ri, "kda")], key=lambda b: b["x0"]
                ):
                    t, s = _rec_text_for(b)
                    if t and s >= 0.5:
                        parts.append(t)
                if parts:
                    kda = _parse_kda_cell(" ".join(parts))
            if kda is None:
                t, _s = rect_results.get((pi, ri, "kda"), ("", 0.0))
                kda = _parse_kda_cell(t)
            if kda is not None:
                n_kda += 1

            # DMG: то же — только чистое неотрицательное целое.
            dmg = None
            dmg_best = -1.0
            for b in cell_members[(pi, ri, "dmg")]:
                t, s = _rec_text_for(b)
                if not t or s < _REC_SCORE_MIN or s <= dmg_best:
                    continue
                d = _parse_dmg_cell(t)
                if d is not None:
                    dmg, dmg_best = d, s
            if dmg is None:
                # DMG, разрезанный det'ом на части («26» + «04») —
                # склейка чисто-цифровых кусков по x.
                parts = []
                for b in sorted(
                    cell_members[(pi, ri, "dmg")], key=lambda b: b["x0"]
                ):
                    t, s = _rec_text_for(b)
                    tt = _WS_RE.sub("", t or "")
                    if tt and s >= 0.5 and _DMG_FULL_RE.match(tt):
                        parts.append(tt)
                if parts:
                    dmg = _parse_dmg_cell("".join(parts))
            if dmg is None:
                t, _s = rect_results.get((pi, ri, "dmg"), ("", 0.0))
                dmg = _parse_dmg_cell(t)
            if dmg is not None:
                n_dmg += 1

            # Name: разделение строк по cy — верхняя = ник,
            # нижняя = клан/гильдия/водяной знак (secondary_text).
            name_items = []
            for b in cell_members[(pi, ri, "name")]:
                if b["idx"] in merged_texts:
                    for it in merged_texts[b["idx"]]:
                        it = dict(it)
                        it["cx"] = b["cx"]
                        name_items.append(it)
                    continue
                t, s = _rec_text_for(b)
                name_items.append({
                    "cx": b["cx"], "cy": b["cy"], "y0": b["y0"],
                    "y1": b["y1"], "text": t, "score": s,
                })
            name, secondary = _cluster_name_lines(name_items, pitch, row_cy)
            if not name:
                t, _s = rect_results.get((pi, ri, "name"), ("", 0.0))
                name = _parse_name_cell([(0.0, (t, _s))])
                secondary = ""
            if name:
                n_names += 1

            players[side].append(
                {
                    "name": name or "???",
                    "secondary_text": secondary or None,
                    "kills": kda[0] if kda else None,
                    "deaths": kda[1] if kda else None,
                    "assists": kda[2] if kda else None,
                    "dmg": dmg,
                    "kd": (
                        round(kda[0] / kda[1], 2)
                        if kda and kda[1]
                        else (float(kda[0]) if kda else None)
                    ),
                    "cy": row_cy,
                    "row_h": rh,
                }
            )

    # --- Этап 9: валидация ---
    if not (n_kda == 8 and n_dmg == 8 and n_names == 8):
        _record_metrics(
            timings, t0, ok=False,
            reason=f"validate kda={n_kda} dmg={n_dmg} names={n_names}",
        )
        return None

    # Все распознанные боксы (headers + cells + верх/низ полосы) —
    # для score-fallback и winner-диагностики downstream.
    rec_boxes: list[dict] = []
    for idx, (text, _score) in rec.cache.items():
        b = box_by_idx.get(idx)
        if b is None or not text:
            continue
        rec_boxes.append(
            {
                "text": text, "x0": b["x0"], "y0": b["y0"],
                "x1": b["x1"], "y1": b["y1"],
                "cx": b["cx"], "cy": b["cy"],
            }
        )

    # Синтетические боксы счёта из score-полос (когда det пропустил
    # мелкие цифры) — parse_match_score сам выберет ближайшие.
    rec_boxes.extend(score_extra)

    # Score «N VS M» — из верхней полосы
    score = parse_match_score(rec_boxes) if rec_boxes else None

    # Баннер / top5 — по всем распознанным текстам
    all_text = " ".join(
        b["text"].upper() for b in rec_boxes
    )
    banner = None
    if "ПОРАЖЕНИЕ" in all_text or "DEFEAT" in all_text or "LOST" in all_text:
        banner = "lose"
    elif (
        "ПОБЕДА" in all_text
        or "BOOYAH" in all_text
        or "BYAH" in all_text
        or "VICTORY" in all_text
    ):
        banner = "win"
    top5 = "ПЯТЕРКУ" in all_text or "ЛУЧШИХ" in all_text

    parsed = {
        "team_a": players["left"],
        "team_b": players["right"],
        "team_a_name": None,
        "team_b_name": None,
        "team_a_name_box": geoms[0]["name_region"],
        "team_b_name_box": geoms[1]["name_region"],
        "top5": top5,
        "banner": banner,
        "score": list(score) if score else None,
        "boundary_x": boundary,
        "img_w": float(img_w),
        "img_h": float(img_h),
        "_rec_boxes": rec_boxes,
    }
    _record_metrics(timings, t0, ok=True)
    return parsed


LAST_FALLBACK_REASON = ""


def last_fallback_reason() -> str:
    """Причина последнего fallback'а fast-пути (для диагностики)."""
    return LAST_FALLBACK_REASON


def _record_metrics(timings: dict, t0: float, *, ok: bool, reason: str = "") -> None:
    global LAST_FALLBACK_REASON
    LAST_FALLBACK_REASON = "" if ok else reason
    # Per-call sink: точная причина для конкретного вызова (диагностика),
    # в отличие от глобальной LAST_FALLBACK_REASON, которая гонится
    # при параллельных прогонах.
    sink = timings.pop("_reason_sink", None)
    if sink is not None and not ok:
        sink.append(reason)
    try:
        from utils import perf_metrics

        total = (time.perf_counter() - t0) * 1000.0
        perf_metrics.record("fast_ocr_total_ms", total)
        for k, v in timings.items():
            perf_metrics.record(f"fast_ocr_{k}", v)
        perf_metrics.incr("fast_ocr_ok" if ok else "fast_ocr_fallback")
        if not ok:
            perf_metrics.incr("fast_ocr_fallback_" + re.sub(r"\W", "_", reason)[:40])
    except Exception:
        pass


async def parse_cs_fast(
    data: bytes, reason_out: list | None = None
) -> dict[str, Any] | None:
    """Async-обёртка: anchor-based fast OCR → parsed dict или None.

    None = fast-путь не сработал → вызывающий код обязан запустить
    полный RapidOCR pipeline (fallback гарантирует точность).
    reason_out — per-call список для причины fallback.
    """
    import asyncio
    import functools

    from services import ocr_worker
    from services.ocr_service import OCR_EXECUTOR

    loop = asyncio.get_running_loop()
    t_submit = time.perf_counter()

    def _run() -> dict[str, Any] | None:
        if ocr_worker.worker_enabled():
            try:
                res = ocr_worker.call("cs_fast", data, retries=1)
            except ocr_worker.OcrWorkerError as exc:
                # ONNX в основной процесс не грузим — fast-путь
                # просто недоступен, полный OCR тоже идёт через
                # worker и завершится ошибкой выше по стеку.
                logger.error(
                    "OCR worker failed after retries: %s", exc
                )
                return None
            if reason_out is not None and res.get("reason"):
                reason_out.append(res["reason"])
            try:
                from utils import perf_metrics

                perf_metrics.record(
                    "fast_ocr_total_ms", res.get("wall_ms", 0.0)
                )
                perf_metrics.incr(
                    "fast_ocr_ok"
                    if res.get("ok")
                    else "fast_ocr_fallback"
                )
            except Exception:
                pass
            return res.get("parsed")
        # Локальный путь — только при явном OCR_WORKER_DISABLE=1.
        return _extract_parsed(data, t_submit, reason_out)

    return await loop.run_in_executor(OCR_EXECUTOR, _run)
