# Hermes Personal

Русскоязычный личный ассистент на Hermes Agent: `gpt-5.6-sol` через
OpenAI Codex OAuth, Telegram, экономный DDGS/Tavily/local-browser роутер,
официальные Kakao Maps API и browser-проверки для Кореи, Google Workspace,
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
sed -i '/^API_SERVER_KEY=/d' .env
openssl rand -hex 32 | sed 's/^/API_SERVER_KEY=/' >> .env
install -d -m 700 runtime credentials backups knowledge/private
chmod 600 .env
```

Заполните в `.env`:

```dotenv
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USERS=
TELEGRAM_HOME_CHANNEL=
TAVILY_API_KEY=
KAKAO_REST_API_KEY=
```

`TELEGRAM_ALLOWED_USERS` — числовой Telegram user ID. Для личного диалога
`TELEGRAM_HOME_CHANNEL` обычно равен тому же ID. `.env` смонтирован read-only,
поэтому production-развёртывание намеренно не позволяет `/sethome` переписывать
файл с секретами: измените значение на хосте и пересоздайте контейнер. Команда
установки заранее создаёт `API_SERVER_KEY`, поскольку контейнер не может сам
дописать его в read-only `.env`. Compose использует этот файл для подстановки
несекретных build/runtime параметров, но не экспортирует весь набор ключей в
container environment; Hermes читает секреты непосредственно из
`/opt/data/.env`.

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

Дополнительные Python-зависимости собраны из `requirements/*.in`, а полный
transitive graph отдельно для Linux amd64/arm64 закреплён SHA-256 в
`requirements/*.lock`. Docker build использует `uv --require-hashes`;
обновляйте `.in` и оба архитектурных lock-файла одним осознанным изменением.

## Граница доверия Telegram

Telegram использует минимальный явный allowlist: web, browser и Korea. Sentinel
`no_mcp` запрещает автоматическую подстановку MCP-серверов. Два локальных
Korea/web skills загружаются явной slash-командой или привязкой Telegram Topic;
опасный `skill_manage` удалённому чату не выдаётся. Также недоступны terminal,
произвольные файловые операции,
Python/code execution, memory/skill management, session search, delegation,
cron и произвольные MCP. Полный `hermes-cli` остаётся только локальным
административным интерфейсом; через него выполняются настройка, диагностика и
  Google Workspace. Read-only Яндекс Почта намеренно не доступна ни в Telegram,
  ни в полном admin CLI: недоверенное письмо не должно делить контекст с
  сетевыми, файловыми или terminal tools.

У локального browser upstream обычно отключает SSRF-фильтр, предполагая, что у
того же пользователя уже есть terminal. Для Telegram это предположение неверно.
Production launcher запускает обязательный loopback egress-proxy и передаёт его
в фактический дочерний процесс Chromium без `DIRECT` fallback. Proxy сам один раз
разрешает DNS-имя, проверяет весь набор адресов и соединяется только с уже
проверенным публичным numeric IP. Поэтому loopback, link-local, RFC1918/LAN,
private redirect, subresource и DNS rebinding блокируются до сетевого запроса;
QUIC и непроксируемый WebRTC отключены. URL/JS-проверки Hermes остаются вторым
слоем, а чувствительные JavaScript-примитивы запрещены через
`browser.restrict_evaluate`. Если proxy или его runtime contract недоступен,
gateway не стартует; если proxy остановится позже, browser теряет сеть без
перехода на прямое соединение. Локальный административный CLI запускается
отдельным процессом и сохраняет свой обычный local-terminal режим.

Запись memory и skills требует явного подтверждения, destructive slash-команды
тоже подтверждаются, а security scanner работает fail-closed. `.env`,
`config.yaml`, весь каталог плагинов, `SOUL.md` и version-controlled skills
смонтированы read-only; OAuth/session state хранится отдельно в `runtime/`.
Tavily, Kakao и Yandex credentials отправляются только на закреплённые
официальные endpoints.

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
обновляются. После подключения откройте отдельную mail-only TUI:

```bash
bash scripts/yandex-mail.sh chat
```

Перед запуском сессия fail-closed перепроверяет регистрацию и точный состав
двух read-only mail tools; при ошибке плагина TUI не откроется. Процесс проходит
штатный container bootstrap и работает от пользователя `hermes`, а не `root`.
В сессии нет web, browser, terminal, file, memory, delegation или MCP. Уже
внутри неё попросите, например: «Покажи пять последних писем из Входящих».

У Яндекса нет отдельного OAuth scope только для папки «Входящие»:
`mail:imap_ro` разрешает чтение всего ящика. Ограничение до `INBOX` обеспечивает
сам плагин Hermes. Содержимое вложений не передаётся модели; видны только их
имена и MIME-типы. Письма крупнее безопасного лимита плагин читать отказывается.

## Душа и операционные skills

`SOUL.md` содержит только личность, стиль общения и постоянные пользовательские
предпочтения. Предметные процедуры загружаются по необходимости из двух
version-controlled skills:

- `cost-aware-web-routing` — выбор DDGS, Tavily и локального browser;
- `korea-local-operations` — места, маршруты, покупки и приватная геолокация.

Каталог `./skills` смонтирован read-only как `/opt/data/custom-skills` и подключён
через `skills.external_dirs`. Он не перекрывает управляемый Hermes каталог
`/opt/data/skills` со встроенными skills. После изменения skill перезапустите
gateway, выполните `/reset` и проверьте discovery командой
`docker compose exec hermes hermes skills list`.

В обычном личном Telegram DM skills вызываются явно, без выдачи write-capable
toolset `skills`:

```text
/cost_aware_web_routing найди актуальную документацию по ...
/korea_local_operations где рядом поесть рамён и как пройти
```

После первого вызова skill остаётся в истории текущей сессии. Если в Telegram
включены DM Topics, эти же имена можно привязать полем `skill` к отдельным темам,
и Hermes будет безопасно загружать их при создании новой topic-сессии.

## Веб-поиск без незаметного расхода Tavily

Backend классифицирует намерение до запроса и возвращает метку
`provider_used` с фактически использованным провайдером:

- обычные факты, документация, новости и технические запросы идут сначала в
  DDGS;
- явно сложное исследование идёт в Tavily `advanced`;
- слабая DDGS-выдача оценивается по числу уникальных пригодных результатов,
  наличию сниппетов и релевантности; только после этого разрешена эскалация в
  Tavily `basic`;
- одиночный URL не отправляется ни в DDGS, ни в Tavily: backend рекомендует
  локальный browser;
- `web_extract` — отдельное явное платное действие через Tavily; один вызов
  ограничен 20 URL и создаёт не более одного платного запроса, а больший пакет
  отклоняется до обращения к API. Для конкретной или динамической страницы
  сначала используется локальный browser.

Встроенные в Hermes внешние `keyless_rescue` и `keyless_fallback` отключены:
они не могут обойти этот роутер при ошибке. Все переключения между DDGS и
Tavily происходят только по описанной выше политике и отражаются в
`provider_used`/`attempted_providers`.

В метаданных также видны тип запроса, причина маршрута, качество DDGS,
эскалация/fallback и факт обращения к Tavily. Поэтому техническая ошибка больше
не является единственным условием переключения, а простой запрос не может
незаметно стать Tavily-запросом только из-за формулировки вроде «сравни».

## Корея: Kakao Maps и локальный browser

Плагин `plugins/korea` использует только официальные Kakao HTTP API. Реальный ключ
хранится только в `/opt/data/.env` (в этом проекте это закрытый `.env`,
смонтированный Docker Compose); не кладите его в `config.yaml`, SOUL, память или
исходники.

1. В [Kakao Developers](https://developers.kakao.com/) создайте приложение,
   активируйте Kakao Map API и скопируйте REST API key в
   `KAKAO_REST_API_KEY`. Проверьте актуальную квоту и настройки приложения в
   [документации Kakao Map REST API](https://developers.kakao.com/docs/en/kakaomap/rest-api).
2. Для автомобильных маршрутов используется официальный
   [Kakao Mobility Directions API](https://developers.kakaomobility.com/guide/navi-api/directions),
   для пеших и общественного транспорта — Kakao Map routing.
3. После изменения `.env` пересоздайте контейнер: `docker compose up -d --force-recreate`.

Одноразовая проверка всех шести endpoints запускается командой
`python scripts/check-kakao-api.py`: ключ вводится скрыто, остаётся только в RAM
процесса и не записывается в проект.

Доступны инструменты `korea_place_search`, `korea_geocode`,
`korea_reverse_geocode`, `korea_route`, `korea_shopping_search` и
`korea_kakao_links`. `korea_shopping_search` не вызывает API, а только готовит
переход к поиску через локальный browser. KakaoMap-ссылки строятся по
[официальному URL API](https://apis.map.kakao.com/web/guide/). Для Kakao T
возвращается только официальный launcher: публичного документированного URL с
заранее заполненным пунктом назначения нет.

Kakao Local не отдаёт часы работы, `open now`, меню, цены, количество/свежесть
отзывов или остатки. Hermes проверяет доступные поля локальным browser на
странице места либо сайте сети и отмечает источник и время проверки; всё
остальное остаётся «не проверено». Naver API в проекте отсутствует. Для товаров
Hermes открывает поиск и сайты сетей локальным browser. «Есть в каталоге» никогда
не обозначается как подтверждённое наличие на полке.

### Геолокация Telegram

Hermes v2026.8.27 принимает обычную статическую геометку и venue из Telegram.
Отправьте боту точку через меню вложений; Live Location пока не используйте.
Плагин перехватывает событие до записи текста в Hermes `state.db`, удаляет из
текста координаты и Google Maps URL и заменяет их случайным токеном. Координаты
живут только в RAM процесса ограниченное время, используются лишь Korea-tools и
исчезают по TTL/перезапуску; в память и ответы они не записываются. Telegram как
внешний сервис всё равно получает и хранит отправленную ему геометку — это не
скрывает координаты от самого Telegram.

Для такого токена reverse geocode текущей точки заблокирован, чтобы точный адрес
не попал в историю. Поиск рядом возвращает только крупный диапазон расстояния и
относительный ранг, а не точные расстояния до нескольких мест; выбранный маршрут
может вернуть необходимую пользователю оценку пути, не раскрывая origin или
геометрию трека.

Gateway запускается через обязательный fail-closed launcher: он ставит и
проверяет ingress-защиту после штатного profile-bootstrap Hermes, но до CLI
dispatch и запуска Telegram-адаптера. Так Hermes сначала корректно применяет
`--profile`/active profile, а при недоступном плагине/guard процесс завершается и
gateway с незащищённой геолокацией не стартует.
Launcher и Compose принудительно передают `--no-supervise`, поэтому s6 не
переносит gateway в другой Python-процесс без установленной защиты. Флаг
`--external-supervisor` возвращает штатные перезапуски Docker Compose, который
снова запускает тот же fail-closed launcher.

Для запроса «где рядом поесть рамён и как пройти» Hermes переводит намерение на
корейский, ищет через Kakao, проверяет доступные актуальные детали browser-ом и
строит маршрут от эфемерной точки. Ответ содержит корейское и русское название,
копируемый адрес,
проверенные часы/цену/отзывы либо честную отметку об отсутствии данных,
станцию/остановку только если она подтверждена, расстояние и KakaoMap-ссылку.

## Запуск

```bash
docker compose up -d
docker compose ps
docker compose exec hermes hermes status
docker compose logs --tail=100 hermes
```

Напишите боту, затем отправьте `/reset` для чистой сессии. Если позже меняете
`TELEGRAM_HOME_CHANNEL` в `.env`, выполните
`docker compose up -d --force-recreate`. Логи в реальном времени:

```bash
docker compose logs -f hermes
```

## Конфигурация и данные

`SOUL.md` автоматически монтируется как `/opt/data/SOUL.md`; отдельно
передавать его не нужно. После изменения личности отправьте `/reset`.
`config.yaml` содержит настройки модели и инструментов, `memories/` — профиль
и долговременную память, `knowledge/` — Obsidian-vault и LLM Wiki, а
`runtime/` — OAuth, сессии, базы и логи.

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
docker compose run --rm --no-deps \
  --entrypoint /opt/hermes/.venv/bin/hermes hermes config check

# Явно останавливаем и удаляем прежний контейнер. Не добавляйте `-v`:
# постоянные данные при редеплое удалять нельзя.
docker compose down --remove-orphans --timeout 120
docker compose up -d --remove-orphans
docker compose ps
docker compose logs --tail=100 hermes
```

В этой сборке у gateway только один владелец жизненного цикла — защищённый
Docker `CMD`. Автовосстановление профиля через s6 отключено, поэтому сохранённый
`gateway_state.json` не сможет поднять второй Telegram poller параллельно.
