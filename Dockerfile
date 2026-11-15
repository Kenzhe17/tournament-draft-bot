# syntax=docker/dockerfile:1
#
# Образ для двух ролей (выбор через SERVICE_ROLE):
#   bot   (default) — python bot.py
#   panel           — uvicorn control_center.main:app :8080
#                     (API + собранная React-панель panel/dist)

# --- Stage 1: React-сборка панели ---
FROM node:22-alpine AS panel-build
WORKDIR /panel
COPY panel/package.json panel/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY panel/ ./
RUN npm run build

# --- Stage 2: Python runtime ---
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
RUN mkdir -p /app/data

COPY requirements.txt control_center/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r control_center/requirements.txt

COPY bot.py config.py ./
COPY cogs/ cogs/
COPY services/ services/
COPY models/ models/
COPY storage/ storage/
COPY utils/ utils/
COPY views/ views/
COPY ultimatum/ ultimatum/
COPY control_center/ control_center/
COPY assets/ assets/
COPY assets_ds_server/ assets_ds_server/
COPY content/ content/
COPY --from=panel-build /panel/dist panel/dist

EXPOSE 8080

# SERVICE_ROLE=panel → Control Center; иначе — Discord-бот.
CMD ["sh", "-c", "if [ \"$SERVICE_ROLE\" = \"panel\" ]; then exec uvicorn control_center.main:app --host 0.0.0.0 --port ${PORT:-8080}; else exec python bot.py; fi"]
