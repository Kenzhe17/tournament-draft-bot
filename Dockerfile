FROM python:3.12-slim



WORKDIR /app



ENV PYTHONUNBUFFERED=1

ENV DATA_DIR=/app/data

RUN mkdir -p /app/data

# OpenCV (PaddleOCR) requires these system libs on slim images
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 libgfortran5 libsndfile1 \
    && rm -rf /var/lib/apt/lists/*



COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Chromium для рендера PNG-профиля (Playwright)
RUN playwright install --with-deps chromium



COPY bot.py config.py ./

COPY cogs/ cogs/

COPY services/ services/

COPY models/ models/

COPY storage/ storage/

COPY utils/ utils/

COPY views/ views/

COPY ultimatum/ ultimatum/

COPY assets/ assets/

COPY assets_ds_server/ assets_ds_server/

COPY content/ content/

# Pre-download RapidOCR ONNX models into the image.
# v1 (rapidocr_onnxruntime) создаёт сессии eagerly — warmup хватает.
# v3 (rapidocr, кириллица) — ЛЕНИВО: rec-сессия создаётся только при
# первом распознанном боксе, поэтому warmup на пустом кадре модель
# не скачивал → в Railway она докачивалась в рантайме. Качаем
# детерминированно через штатный download_task + проверяем.
COPY tools/download_ocr_models.py tools/
RUN python -c "from rapidocr_onnxruntime import RapidOCR; import numpy as np; o=RapidOCR(); o(np.zeros((64,64,3),dtype='uint8'))"
RUN python tools/download_ocr_models.py
RUN python -c "import sys; from services.ocr_service import verify_model_files; m=verify_model_files(); print('missing models:', m); sys.exit(1 if m else 0)"



CMD ["python", "bot.py"]

