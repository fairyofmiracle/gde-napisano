#!/usr/bin/env bash
# Скачивает корневой сертификат НУЦ Минцифры (нужен для GigaChat) в ./certs/russian_trusted_root_ca.pem.
# Сертификат публичный; после скачивания сверьте отпечаток SHA-256 с опубликованным на Госуслугах
# (https://www.gosuslugi.ru/crt). docker-compose монтирует ./certs в контейнер, приложение находит файл само.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p certs
URL="${RUSSIAN_CA_URL:-https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt}"
tmp="$(mktemp)"
curl -fsSL "$URL" -o "$tmp"
if ! grep -q "BEGIN CERTIFICATE" "$tmp"; then
  # если пришёл DER, переводим в PEM
  openssl x509 -inform der -in "$tmp" -out "$tmp.pem" && mv "$tmp.pem" "$tmp"
fi
openssl x509 -in "$tmp" -noout -subject -enddate
echo -n "SHA-256: "; openssl x509 -in "$tmp" -noout -fingerprint -sha256 | cut -d= -f2
mv "$tmp" certs/russian_trusted_root_ca.pem
chmod 644 certs/russian_trusted_root_ca.pem
echo "Сохранено: certs/russian_trusted_root_ca.pem"
