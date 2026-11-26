#!/usr/bin/env bash
# Восстановление из копии scripts/backup.sh.
#
#   scripts/restore.sh backups/gde-napisano-20261128-0330.tar.gz [backups/qdrant-20261128-0330.tar.gz]
#
# Без архива Qdrant индекс пересоберётся из документов при старте приложения (несколько минут).
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${COMPOSE_PROJECT_NAME:-gde-napisano}"
APP_ARCHIVE="${1:?Укажите архив gde-napisano-*.tar.gz}"
QDRANT_ARCHIVE="${2:-}"
APP_ARCHIVE="$(cd "$(dirname "$APP_ARCHIVE")" && pwd)/$(basename "$APP_ARCHIVE")"

# Без Docker: приложение остановите сами, затем
#   NATIVE_DATA_DIR=/путь/к/DATA_DIR scripts/restore.sh backups/gde-napisano-….tar.gz
if [[ -n "${NATIVE_DATA_DIR:-}" ]]; then
  find "$NATIVE_DATA_DIR" -mindepth 1 -maxdepth 1 ! -name qdrant -exec rm -rf {} +
  tar xzf "$APP_ARCHIVE" -C "$NATIVE_DATA_DIR"
  if [[ -f "$NATIVE_DATA_DIR/backup.sqlite3" ]]; then
    mv "$NATIVE_DATA_DIR/backup.sqlite3" "$NATIVE_DATA_DIR/docagent.sqlite3"
    rm -f "$NATIVE_DATA_DIR"/docagent.sqlite3-wal "$NATIVE_DATA_DIR"/docagent.sqlite3-shm
  fi
  echo "данные восстановлены из $APP_ARCHIVE в $NATIVE_DATA_DIR; запустите приложение (индекс пересоберётся из документов)"
  exit 0
fi

docker compose stop app bot 2>/dev/null || docker compose stop app
docker volume create "${PROJECT}_app-data" >/dev/null
docker run --rm -v "${PROJECT}_app-data:/data" -v "$(dirname "$APP_ARCHIVE"):/backup:ro" alpine:3.20 sh -c \
  "find /data -mindepth 1 -delete && tar xzf /backup/$(basename "$APP_ARCHIVE") -C /data \
   && if [ -f /data/backup.sqlite3 ]; then mv /data/backup.sqlite3 /data/docagent.sqlite3; rm -f /data/docagent.sqlite3-wal /data/docagent.sqlite3-shm; fi \
   && chown -R 1000:1000 /data"
echo "app-data восстановлен из $APP_ARCHIVE"

if [[ -n "$QDRANT_ARCHIVE" ]]; then
  QDRANT_ARCHIVE="$(cd "$(dirname "$QDRANT_ARCHIVE")" && pwd)/$(basename "$QDRANT_ARCHIVE")"
  docker compose stop qdrant
  docker volume create "${PROJECT}_qdrant-data" >/dev/null
  docker run --rm -v "${PROJECT}_qdrant-data:/q" -v "$(dirname "$QDRANT_ARCHIVE"):/backup:ro" alpine:3.20 sh -c \
    "find /q -mindepth 1 -delete && tar xzf /backup/$(basename "$QDRANT_ARCHIVE") -C /q"
  echo "qdrant-data восстановлен из $QDRANT_ARCHIVE"
fi

docker compose up -d
echo "Проверка: curl -s http://127.0.0.1:${APP_PORT:-8010}/ready"
