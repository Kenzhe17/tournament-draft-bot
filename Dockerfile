FROM python:3.12-slim



WORKDIR /app



ENV PYTHONUNBUFFERED=1

ENV DATA_DIR=/app/data



RUN mkdir -p /app/data

# Install system dependencies for OpenCV
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*



COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt



COPY bot.py config.py ./

COPY cogs/ cogs/

COPY models/ models/

COPY storage/ storage/

COPY utils/ utils/

COPY views/ views/

COPY services/ services/



CMD ["python", "bot.py"]

