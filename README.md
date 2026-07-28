# Hermes Personal

Русскоязычный личный ассистент на Hermes Agent: `gpt-5.6-sol` через
OpenAI Codex OAuth, Telegram, Tavily с DDGS fallback, Google Workspace,
локальная память, Obsidian/LLM Wiki и голосовые сообщения.

## Требования

Нужен Linux-сервер с Git, Docker Engine и Docker Compose v2. Рекомендуется
2 vCPU, 4 ГБ RAM и не менее 10 ГБ свободного диска. Docker должен запускаться
обычным пользователем без `sudo`; Hermes не публикует входящие порты и требует
только исходящий HTTPS.

## Установка

```bash
git clone <REPO_URL> hermes-personal
cd hermes-personal

test "$(id -u)" -ne 0 || {
  echo "Запускайте Hermes от обычного пользователя"
  exit 1
}

cp .env.example .env
sed -i "s/^HERMES_UID=.*/HERMES_UID=$(id -u)/" .env
sed -i "s/^HERMES_GID=.*/HERMES_GID=$(id -g)/" .env
install -d -m 700 runtime credentials backups knowledge/private
chmod 600 .env
```

Заполните в `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=
TAVILY_API_KEY=
```

`TELEGRAM_ALLOWED_USERS` — числовой Telegram user ID. Домашний чат заранее
указывать не нужно: после запуска команда `/sethome` сохранит его в `.env`.

Соберите образ, авторизуйте Codex и проверьте конфигурацию:

```bash
docker compose config --quiet
docker compose build --pull
docker compose run --rm hermes auth add openai-codex --no-browser
docker compose run --rm hermes config check
docker compose run --rm hermes memory status
docker compose run --rm hermes chat -q \
  "Ответь по-русски одним словом: готов"
```

OAuth-токен сохраняется в `runtime/` и не попадает в Git.

## Google Workspace

В Google Cloud включите Gmail API, Google Calendar API, Google Drive API,
Google Sheets API, Google Docs API и People API. Создайте OAuth Client типа
**Desktop app**, скачайте JSON и положите его в
`credentials/google-client-secret.json`.

```bash
chmod 600 credentials/google-client-secret.json
bash scripts/google-workspace.sh install-client
bash scripts/google-workspace.sh auth-url
```

Откройте выданный URL. Переход на `http://localhost:1` завершится ошибкой —
это ожидаемо. Скопируйте полный URL из адресной строки:

```bash
bash scripts/google-workspace.sh auth-code \
  'ПОЛНЫЙ_URL_ИЗ_АДРЕСНОЙ_СТРОКИ'
bash scripts/google-workspace.sh check-live
```

## Запуск

```bash
docker compose up -d
docker compose ps
docker compose exec hermes hermes status
docker compose logs --tail=100 hermes
```

Напишите боту, отправьте `/sethome`, затем `/reset` для чистой сессии. Логи в
реальном времени:

```bash
docker compose logs -f hermes
```

## Конфигурация и данные

`SOUL.md` автоматически монтируется как `/opt/data/SOUL.md`; отдельно
передавать его не нужно. После изменения личности отправьте `/reset`.
`config.yaml` содержит настройки модели и инструментов, `memories/` — профиль
и долговременную память, `knowledge/` — Obsidian-vault и LLM Wiki, а
`runtime/` — OAuth, сессии, базы и логи.

Поиск сначала использует Tavily `basic`. При лимите, rate limit, HTTP 408/5xx
или сетевой ошибке запрос повторяется через DDGS. Извлечение содержимого URL
остаётся на Tavily, потому что DDGS поддерживает только поиск.

После изменения `config.yaml` или плагина перезапустите сервис:

```bash
docker compose restart hermes
```

## Резервная копия и обновление

```bash
docker compose exec hermes hermes backup \
  -o "/backup/hermes-$(date +%F-%H%M%S).zip"
```

Архив содержит секреты и должен храниться зашифрованно.

```bash
git pull --ff-only
docker compose config --quiet
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
docker compose logs --tail=100 hermes
```
