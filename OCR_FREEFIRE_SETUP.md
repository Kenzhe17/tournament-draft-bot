# Free Fire OCR — настройка battles-recon

Бот вызывает battles-recon (`POST /process`) с `game=freefire`, `mode=squad`.
Профиль ищется как `profiles/freefire_squad.json` внутри сервиса battles-recon.

## Что уже есть

- `ocr/profiles/freefire_squad.json` — стартовый профиль (колонки:
  placement / name / kills). x-координаты — **приблизительные**, их нужно
  откалибровать по реальному скриншоту.
- `arbiter/services/ocr_service.py` — клиент (`OCR_SERVICE_URL`, таймаут 120с).
- `/arb match ocr` и `/arb ocr` — команды в боте.

## Шаги настройки

1. **Скриншоты**: сделай 5–10 скриншотов экрана результатов FF
   (Squad/Craftland — что играете) в разных разрешениях.
   Важно: экран должен содержать таблицу с никами и киллами.

2. **Откалибруй профиль** `profiles/freefire_squad.json`:
   - `x_start_pct`/`x_end_pct` каждой колонки — доли ширины изображения.
     Открой скриншот, прикинь где начинаются/кончаются колонки
     (0.0 = левый край, 1.0 = правый).
   - Если у FF-экрана две команды (свои/чужие) — профиль читает все строки;
     бот сам сопоставит ники с ростером.
   - `expected_rows` — число строк в таблице (обычно 4 на команду, до 8).
   - `kills` может быть не единственной цифрой — при необходимости добавь
     колонки `deaths`, `assists`, `damage` по аналогии (нужны их x-диапазоны).

3. **Задеплой battles-recon** (Docker):
   ```bash
   git clone https://github.com/TeamBattles/battles-recon.git
   cp freefire_squad.json battles-recon/profiles/
   docker-compose -f docker/docker-compose.yml --profile tesseract up -d
   # или --profile multi для Tesseract+RapidOCR
   ```
   Профиль также можно монтировать volume'ом — тогда не нужно копировать.

4. **Railway переменные бота**:
   ```
   OCR_SERVICE_URL=http://<host>:8000
   FF_OCR_GAME=freefire
   FF_OCR_MODE=squad
   ```

5. **Проверка**:
   ```bash
   curl http://<host>:8000/health
   curl http://<host>:8000/profiles   # должен быть freefire:squad
   ```
   Затем в Discord: `/arb match ocr` со скриншотом.

## Отладка

- `DEBUG_IMAGES=true` в battles-recon — сохраняет промежуточные
  изображения pipeline (видно, что OCR «видит»).
- `POST /process` с `engines=tesseract` для проверки одного движка.
- Если ники читаются плохо — добавь `language_packs` и при необходимости
  обучи модель: `bash training/scripts/train.sh freefire squad`
  (см. README battles-recon → Training).
