#!/usr/bin/env bash
# Скачивает модели поиска в том hf-models до первого запуска, чтобы стенд не зависел от huggingface.co
# при перезапуске. Запускать из папки проекта на сервере: scripts/prefetch_models.sh
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose build app
docker compose run --rm --no-deps -e HF_HUB_OFFLINE=0 app python scripts/prefetch_models.py
echo "Модели в томе hf-models. Чтобы не ходить в интернет при старте, добавьте в .env: HF_HUB_OFFLINE=1"
