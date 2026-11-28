#!/bin/sh
# Резервная копия постоянных хранилищ бота (DATA_DIR + опционально Postgres).
# Запускать внутри контейнера/на хосте production ДО деплоя.
#
#   sh scripts/backup_data.sh
#
# Результат: $BACKUP_DIR/botdata-<timestamp>/ с data.tar.gz и db.sql.
set -e

TS=$(date +%Y%m%d-%H%M%S)
OUT="${BACKUP_DIR:-/tmp}/botdata-$TS"
mkdir -p "$OUT"

if [ -n "$DATA_DIR" ] && [ -d "$DATA_DIR" ]; then
    tar -czf "$OUT/data.tar.gz" -C "$DATA_DIR" .
    echo "DATA_DIR ($DATA_DIR) -> $OUT/data.tar.gz"
else
    echo "DATA_DIR не задан или не существует — пропуск"
fi

if [ -n "$DATABASE_URL" ]; then
    if command -v pg_dump >/dev/null 2>&1; then
        pg_dump "$DATABASE_URL" > "$OUT/db.sql" \
            && echo "Postgres -> $OUT/db.sql" \
            || echo "pg_dump завершился с ошибкой — проверьте db.sql"
    else
        echo "pg_dump недоступен в контейнере — дамп БД сделайте извне"
    fi
else
    echo "DATABASE_URL не задан — файловое хранилище, дамп не нужен"
fi

echo "Backup dir: $OUT"
