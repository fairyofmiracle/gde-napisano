# Где написано?

Ответы на вопросы по корпоративным документам **со ссылкой на конкретный пункт**.

Вопрос → поиск по пунктам (эмбеддинги + реранкер) → ответ модели только по найденному тексту → в ответе ссылка вида «п. 3.2 Регламента…». Есть веб-интерфейс, админка, операторская и Telegram-бот (текст и голос).

## Возможности

- Разбор документов на пункты: `.docx`, `.doc`, `.pdf`, изображения (OCR), `.txt`/`.md`, `.rtf`, `.odt`, `.xlsx`, `.html`
- Поиск: `multilingual-e5-base` + реранкер `bge-reranker-v2-m3`, пороги уверенности и предупреждение при неоднозначности
- LLM: **Ollama** (по умолчанию) или **GigaChat**
- Переформулировка неуверенного запроса, сверка чисел/дат с текстом пункта
- Отзывы и кейсы «как решили на практике» (с модерацией в операторской)
- Админка: загрузка документов, доступ по токену, IP-фильтрам и rate limit
- Telegram-бот: текст и голосовые (GigaAM или Whisper)

## Структура репозитория

```
app/           # FastAPI-приложение, парсинг, индекс, LLM
bot/           # Telegram-бот (отдельный процесс / образ)
tests/         # pytest
certs/         # корневой сертификат НУЦ для GigaChat (см. certs/README.md)
data/runtime/  # документы, Qdrant, рантайм (в git не попадает)
```

## Быстрый старт (локально)

### 1. Окружение

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

Варианты зависимостей:

| Файл | Когда |
|------|--------|
| `requirements.txt` | Полное приложение (модели, поиск, API) |
| `requirements-ui.txt` | Только UI без torch/моделей (`DEMO_UI_ONLY=true`) |
| `requirements-bot.txt` | Telegram-бот + распознавание речи |
| `requirements-dev.txt` | Полный набор + pytest и тестовые утилиты |

Системные утилиты по необходимости:

- **Tesseract** (`rus+eng`) — OCR для сканов и картинок; на Windows при необходимости задайте `TESSERACT_CMD`
- **LibreOffice** (`soffice`) — конвертация `.doc` → `.docx`; при необходимости `SOFFICE_CMD`
- **Ollama** — если `LLM_PROVIDER=ollama` (модель по умолчанию `qwen2.5:3b-instruct`)

### 2. Конфиг

```powershell
copy .env.example .env
```

Минимум для начала работы:

1. Сгенерируйте токен админки (без него загрузка документов и операторская закрыты):

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(24))"
   ```

   Впишите в `ADMIN_TOKEN` (не короче `ADMIN_TOKEN_MIN_LENGTH`).

2. Проверьте пути: `DATA_DIR`, `DOCS_DIR`, `QDRANT_PATH` (локально Qdrant работает из папки, сервер не обязателен).

3. При GigaChat: `GIGACHAT_CREDENTIALS` и сертификат — см. [certs/README.md](certs/README.md).

Полный список переменных — в `.env.example` с комментариями.

### 3. Запуск

После появления точек входа в коде типичный запуск будет таким:

```powershell
# API / веб
uvicorn app.main:app --host 0.0.0.0 --port 8010

# бот (нужен TELEGRAM_BOT_TOKEN и живой DOCAGENT_URL)
python -m bot
```

Проверка тестов:

```powershell
pip install -r requirements-dev.txt
pytest
```

## Docker (когда появятся compose-файлы)

В `.env.example` уже заложен публичный стенд:

- `PUBLIC_BIND` — `127.0.0.1` только с сервера, `0.0.0.0` — снаружи
- `SITE_ADDRESS` — домен для HTTPS (Let's Encrypt) или пусто для HTTP на порту 80
- профиль бота: `docker compose --profile bot up -d --build`

В образе приложения лежат зависимости из `requirements.txt`; бот собирается отдельно (`requirements-bot.txt`).

## Как пользоваться

1. Откройте веб-интерфейс, задайте вопрос обычным языком.
2. В ответе будет формулировка **и** ссылка на пункт документа.
3. В админке (с `ADMIN_TOKEN`) загрузите/обновите документы — индекс перестроится.
4. В операторской можно разбирать неуверенные ответы и одобрять практические кейсы.
5. В Telegram тот же поиск: текст или голосовое (если включён `TRANSCRIBER`).

## Важные настройки

| Тема | Переменные |
|------|------------|
| LLM | `LLM_PROVIDER`, `OLLAMA_*`, `GIGACHAT_*`, `LLM_RETRIES`, `LLM_MAX_CONCURRENCY` |
| Поиск | `EMBED_MODEL`, `RERANKER_MODEL`, `CONFIDENT_THRESHOLD`, `UNCERTAIN_THRESHOLD`, `AMBIGUITY_MARGIN` |
| Агент | `AGENT_REWRITE`, `AGENT_MAX_REWRITES`, `CHECK_NUMBERS` |
| Безопасность | `ADMIN_TOKEN`, `ADMIN_ALLOWED_IPS`, `ADMIN_*_FAILURES/LOCKOUT`, `RATE_LIMIT_*`, `CORS_ORIGINS` |
| Загрузка | `MAX_UPLOAD_MB`, `ALLOWED_UPLOAD_EXTENSIONS` |
| Бот | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `DOCAGENT_URL`, `TRANSCRIBER` |
| Демо UI | `DEMO_UI_ONLY=true` — интерфейс без тяжёлых моделей |

## Лицензия

MIT — см. [LICENSE](LICENSE).
