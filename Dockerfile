FROM python:3.12-slim



WORKDIR /app



ENV PYTHONUNBUFFERED=1

ENV DATA_DIR=/app/data

RUN mkdir -p /app/data

# OpenCV (PaddleOCR) requires these system libs on slim images
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*



COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt



COPY bot.py config.py ./

COPY cogs/ cogs/

COPY services/ services/

COPY models/ models/

COPY storage/ storage/

COPY utils/ utils/

COPY views/ views/

COPY ultimatum/ ultimatum/

COPY assets/ assets/

# Pre-download PaddleOCR models (ru) into the image
RUN python -c "from paddleocr import PaddleOCR; from PIL import Image; import numpy as np; o=PaddleOCR(lang='ru',use_doc_orientation_classify=False,use_doc_unwarping=False,use_textline_orientation=False,device='cpu'); o.predict(np.zeros((64,64,3),dtype='uint8'))"



CMD ["python", "bot.py"]

