# Hermes Personal

Русскоязычный личный ассистент на Hermes Agent: `gpt-5.6-sol` через
OpenAI Codex OAuth, Telegram, Tavily с DDGS fallback, Google Workspace,
Яндекс Почта только для чтения, локальная память, Obsidian/LLM Wiki и
голосовые сообщения.

## Требования

Нужен Linux-сервер с Git, Docker Engine и Docker Compose v2. Рекомендуется
2 vCPU, 4 ГБ RAM и не менее 10 ГБ свободного диска. Docker должен запускаться
обычным пользователем без `sudo`; Hermes не публикует входящие порты и требует
только исходящие HTTPS и IMAPS (`tcp/993`).

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
bash scripts/google-workspace.sh install-client
bash scripts/google-workspace.sh auth-url
```

При запуске от `root` скрипт сам назначит каталогу и JSON владельца из
`HERMES_UID:HERMES_GID`, сохранив закрытые права `700/600`.

Откройте выданный URL. Переход на `http://localhost:1` завершится ошибкой —
это ожидаемо. Скопируйте полный URL из адресной строки:

```bash
bash scripts/google-workspace.sh auth-code \
  'ПОЛНЫЙ_URL_ИЗ_АДРЕСНОЙ_СТРОКИ'
bash scripts/google-workspace.sh check-live
```

## Яндекс Почта — только чтение

Интеграция использует OAuth, а не пароль от аккаунта. На стороне Яндекса токен
ограничен scope `mail:imap_ro`; Hermes дополнительно открывает только `INBOX` в
режиме `EXAMINE` и читает через `BODY.PEEK`, поэтому письма не помечаются как
прочитанные. Инструментов отправки, удаления, перемещения и изменения флагов в
плагине нет.

1. В настройках Яндекс Почты в разделе «Почтовые программы» включите IMAP и
   «Пароли приложений и OAuth-токены».
2. [Создайте OAuth-приложение](https://oauth.yandex.ru/client/new/id/) типа
   «Для авторизации пользователей». Для платформы «Веб-сервисы» укажите
   Redirect URI `https://oauth.yandex.ru/verification_code` и выберите только
   право «Доступ на чтение писем в почтовом ящике» (`mail:imap_ro`). Не
   добавляйте `mail:imap_full` или `mail:smtp`.
3. Создайте закрытый файл с Client ID, Client secret и полным адресом ящика:

```bash
cp credentials/yandex-mail-oauth.example.json \
  credentials/yandex-mail-oauth.json
chmod 600 credentials/yandex-mail-oauth.json
# Заполните credentials/yandex-mail-oauth.json
```

Получите код, обменяйте его на OAuth- и refresh-токены и проверьте соединение:

```bash
bash scripts/yandex-mail.sh auth-url
# Откройте напечатанный URL и скопируйте показанный Яндексом код.
bash scripts/yandex-mail.sh auth-code
# Вставьте код в закрытый интерактивный запрос.
bash scripts/yandex-mail.sh check-live
```

Токены сохраняются с правами `600` в `runtime/yandex-mail/` и автоматически
обновляются. После подключения перезапустите Hermes и попросите, например:
«Покажи пять последних писем из Входящих».

У Яндекса нет отдельного OAuth scope только для папки «Входящие»:
`mail:imap_ro` разрешает чтение всего ящика. Ограничение до `INBOX` обеспечивает
сам плагин Hermes. Содержимое вложений не передаётся модели; видны только их
имена и MIME-типы. Письма крупнее безопасного лимита плагин читать отказывается.

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
