FROM python:3.12-slim



WORKDIR /app



ENV PYTHONUNBUFFERED=1

ENV DATA_DIR=/app/data



RUN mkdir -p /app/data



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

