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

# Pre-download RapidOCR ONNX models into the image
RUN python -c "from rapidocr_onnxruntime import RapidOCR; import numpy as np; o=RapidOCR(); o(np.zeros((64,64,3),dtype='uint8'))"
RUN python -c "from services.ocr_service import _get_cyrillic_ocr; import numpy as np; _get_cyrillic_ocr()(np.zeros((64,64,3),dtype='uint8'))"



CMD ["python", "bot.py"]

