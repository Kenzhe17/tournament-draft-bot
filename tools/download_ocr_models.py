"""Build-time: скачать ONNX-модели RapidOCR v3 в site-packages.

RapidOCR v3 создаёт det/cls/rec сессии ЛЕНИВО при первом вызове —
warmup на пустом кадре может не создать rec-сессию (det не находит
боксов → rec не инициализируется → модель не скачивается и не
попадает в Docker-образ). Этот скрипт скачивает файлы напрямую
через штатный download_task — детерминированно, без инференса.

Запускается в Dockerfile после pip install. Проверка результата —
services.ocr_service.verify_model_files() (вызывается и в warmup
при старте бота).
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    from rapidocr.inference_engine.base import FileInfo
    from rapidocr.utils.download_models import download_task, root_dir
    from rapidocr.utils.typings import (
        EngineType,
        LangRec,
        ModelType,
        OCRVersion,
        TaskType,
    )

    cache_dir = Path(root_dir) / "models"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Параметры соответствуют services/ocr_service._get_cyrillic_ocr:
    # Det/Cls — дефолты config.yaml (PP-OCRv6 det small,
    # PP-OCRv4 cls mobile), Rec — PPOCRV5 + CYRILLIC + MOBILE.
    needed = [
        FileInfo(
            EngineType.ONNXRUNTIME, OCRVersion.PPOCRV6,
            TaskType.DET, "ch", ModelType.SMALL,
        ),
        FileInfo(
            EngineType.ONNXRUNTIME, OCRVersion.PPOCRV4,
            TaskType.CLS, "ch", ModelType.MOBILE,
        ),
        FileInfo(
            EngineType.ONNXRUNTIME, OCRVersion.PPOCRV5,
            TaskType.REC, LangRec.CYRILLIC, ModelType.MOBILE,
        ),
    ]
    for fi in needed:
        label = (
            f"{fi.task_type.value}/{fi.lang_type}/"
            f"{fi.ocr_version.value}/{fi.model_type.value}"
        )
        try:
            download_task(cache_dir, fi)
        except Exception as exc:
            print(f"FAILED {label}: {exc}", file=sys.stderr)
            return 1
        print(f"ok {label}")
    print(f"Models in {cache_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
