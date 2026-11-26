#!/usr/bin/env bash
# Резервная копия стенда: база SQLite (ответы, отзывы, практика), загруженные документы и, по желанию, Qdrant.
# Индекс Qdrant можно не сохранять: при старте он пересобирается из базы и документов.
#
#   scripts/backup.sh                  # app-data → backups/gde-napisano-ГГГГММДД-ЧЧММ.tar.gz
#   scripts/backup.sh --with-qdrant    # плюс том Qdrant (Qdrant останавливается на время копирования)
#   KEEP=7 scripts/backup.sh           # хранить 7 последних копий (по умолчанию 10)
#   NATIVE_DATA_DIR=/data/dir scripts/backup.sh   # без Docker: копия каталога данных приложения
#
# Для ежедневной копии: crontab -e →  30 3 * * * cd /opt/gde-napisano && scripts/backup.sh >> backups/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${COMPOSE_PROJECT_NAME:-gde-napisano}"
OUT="${BACKUP_DIR:-$PWD/backups}"
KEEP="${KEEP:-10}"
STAMP="$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT"

# Без Docker (приложение запущено напрямую): NATIVE_DATA_DIR=/путь/к/DATA_DIR scripts/backup.sh
if [[ -n "${NATIVE_DATA_DIR:-}" ]]; then
  python3 - "$NATIVE_DATA_DIR" <<'PY'
import sqlite3, sys
d = sys.argv[1]
src = sqlite3.connect(f"{d}/docagent.sqlite3")
dst = sqlite3.connect(f"{d}/backup.sqlite3")
with dst:
    src.backup(dst)
dst.close(); src.close()
print("SQLite: снимок backup.sqlite3 готов")
PY
  tar czf "$OUT/gde-napisano-${STAMP}.tar.gz" -C "$NATIVE_DATA_DIR" --exclude='docagent.sqlite3-*' --exclude=qdrant .
  echo "данные → $OUT/gde-napisano-${STAMP}.tar.gz (индекс Qdrant пересоберётся из документов)"
  ls -1t "$OUT"/gde-napisano-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
  exit 0
fi

# 1. Согласованная копия SQLite средствами самой базы (приложение может продолжать работать)
docker compose exec -T app python - <<'PY'
import sqlite3
src = sqlite3.connect("/data/docagent.sqlite3")
dst = sqlite3.connect("/data/backup.sqlite3")
with dst:
    src.backup(dst)
dst.close(); src.close()
print("SQLite: снимок /data/backup.sqlite3 готов")
PY

# 2. Архив тома app-data (документы + снимок базы)
docker run --rm -v "${PROJECT}_app-data:/data:ro" -v "$OUT:/backup" alpine:3.20 \
  tar czf "/backup/gde-napisano-${STAMP}.tar.gz" -C /data .
echo "app-data → $OUT/gde-napisano-${STAMP}.tar.gz"

# 3. Qdrant по желанию
if [[ "${1:-}" == "--with-qdrant" ]]; then
  docker compose stop qdrant
  trap 'docker compose start qdrant' EXIT
  docker run --rm -v "${PROJECT}_qdrant-data:/q:ro" -v "$OUT:/backup" alpine:3.20 \
    tar czf "/backup/qdrant-${STAMP}.tar.gz" -C /q .
  echo "qdrant-data → $OUT/qdrant-${STAMP}.tar.gz"
fi

# 4. Настройки: .env копируется с правами только для владельца
if [[ -f .env ]]; then
  install -m 600 .env "$OUT/env-${STAMP}.backup"
fi

# 5. Старые копии
ls -1t "$OUT"/gde-napisano-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
ls -1t "$OUT"/qdrant-*.tar.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
ls -1t "$OUT"/env-*.backup 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
echo "Готово. Скопируйте папку backups на другой диск или в объектное хранилище."
