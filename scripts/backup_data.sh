#!/bin/sh
# Резервная копия постоянных хранилищ бота (DATA_DIR + опционально Postgres).
# Запускать внутри контейнера/на хосте production ДО деплоя.
#
#   sh scripts/backup_data.sh
#
# Бэкап пишется НЕ в /tmp (эфемерно): по умолчанию в
#   ${DATA_DIR}/backups/botdata-<timestamp>/  — внутри persistent volume,
#   либо в $BACKUP_DIR, если переменная задана.
#
# ВАЖНО про откат: НЕ восстанавливайте БД целиком из старого бэкапа —
# это уничтожит новые регистрации/результаты/транзакции. Восстанавливайте
# точечно: br_tournaments.json и нужные файлы DATA_DIR из data.tar.gz.
#
# Проверка volume на Railway: `railway volume list` — mount /app/data
# должен существовать; `df -P /app/data` в конце вывода покажет устройство.
set -e

TS=$(date +%Y%m%d-%H%M%S)
OUT="${BACKUP_DIR:-${DATA_DIR:-.}/backups}/botdata-$TS"
mkdir -p "$OUT" || { echo "не удалось создать $OUT" >&2; exit 1; }

BACKED_UP=0

if [ -n "$DATA_DIR" ] && [ -d "$DATA_DIR" ]; then
    tar -czf "$OUT/data.tar.gz" -C "$DATA_DIR" . \
        || { echo "ОШИБКА: tar не смог архивировать $DATA_DIR" >&2; exit 1; }
    # целостность архива
    tar -tzf "$OUT/data.tar.gz" >/dev/null 2>&1 \
        || { echo "ОШИБКА: архив data.tar.gz не читается" >&2; exit 1; }
    BACKED_UP=1
    echo "DATA_DIR ($DATA_DIR) -> $OUT/data.tar.gz"
fi

if [ -n "$DATABASE_URL" ]; then
    if command -v pg_dump >/dev/null 2>&1; then
        pg_dump "$DATABASE_URL" > "$OUT/db.sql" \
            || { echo "ОШИБКА: pg_dump завершился с ошибкой" >&2; exit 1; }
        [ -s "$OUT/db.sql" ] \
            || { echo "ОШИБКА: db.sql пуст" >&2; exit 1; }
        BACKED_UP=1
        echo "Postgres -> $OUT/db.sql"
    else
        echo "ОШИБКА: DATABASE_URL задан, но pg_dump недоступен — " \
             "БД не забэкаплена. Установите клиент или сделайте дамп извне." >&2
        exit 1
    fi
fi

if [ "$BACKED_UP" -eq 0 ]; then
    echo "ОШИБКА: нечего бэкапить (нет DATA_DIR и DATABASE_URL)" >&2
    exit 1
fi

# Манифест: размер + SHA-256 каждого файла (sha256sum, fallback shasum)
(
    cd "$OUT" || exit 1
    : > MANIFEST.txt
    for f in *; do
        [ -f "$f" ] || continue
        size=$(wc -c < "$f" | tr -d ' ')
        if command -v sha256sum >/dev/null 2>&1; then
            sha=$(sha256sum "$f" | cut -d' ' -f1)
        else
            sha=$(shasum -a 256 "$f" | cut -d' ' -f1)
        fi
        printf '%s  %s bytes  sha256:%s\n' "$f" "$size" "$sha" >> MANIFEST.txt
    done
)
echo "--- MANIFEST ---"
cat "$OUT/MANIFEST.txt"
echo "---------------"

# показать устройство каталога бэкапа — сверить с mount persistent volume
df -P "$OUT" | tail -1
echo "Backup dir: $OUT"
