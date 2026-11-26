FROM python:3.12-slim-bookworm

# Tesseract с русским языком для сканов и картинок.
# LibreOffice (для .doc) тяжёлый, ~400 МБ: включается аргументом WITH_LIBREOFFICE=1.
ARG WITH_LIBREOFFICE=0
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus \
    && if [ "$WITH_LIBREOFFICE" = "1" ]; then apt-get install -y --no-install-recommends libreoffice-writer-nogui; fi \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models HF_HUB_DISABLE_TELEMETRY=1 \
    DATA_DIR=/data DOCS_DIR=/data/docs BOXES_CSV=/app/data/boxes.csv \
    FORWARDED_ALLOW_IPS=127.0.0.1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static
COPY scripts ./scripts
COPY data/boxes.csv ./data/boxes.csv

RUN useradd -m -u 1000 appuser && mkdir -p /data /models /certs && chown -R appuser /data /models
USER appuser

# PREFETCH_MODELS=1 — веса e5 и реранкера (~3,4 ГБ) кладутся в образ. Именованный том hf-models при первом
# создании заполняется из образа, так что стенду не нужен доступ к huggingface.co при запуске.
ARG PREFETCH_MODELS=0
RUN if [ "$PREFETCH_MODELS" = "1" ]; then python scripts/prefetch_models.py; fi

EXPOSE 8000
# /ready отвечает 200, когда модели прогреты, а Qdrant и модель ответов доступны
HEALTHCHECK --interval=30s --timeout=6s --start-period=300s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=5)"
# --proxy-headers: адрес клиента берётся из X-Forwarded-For, но только от адресов FORWARDED_ALLOW_IPS (Caddy)
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--timeout-keep-alive", "30", "--limit-concurrency", "64"]
