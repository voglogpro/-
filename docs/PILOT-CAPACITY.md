# Ограничения нагрузки пилота

Версия v2.10 запускается одним процессом с SQLite. Ограничители ниже защищают
управляемый пилот от резкого наплыва, но не превращают SQLite в горизонтально
масштабируемую production-базу.

## API и фотографии

До чтения JSON приложение резервирует один из ограниченных process-local
слотов:

- `API_READ_INFLIGHT_MAX=32` — одновременные GET/HEAD API-запросы;
- `API_WRITE_INFLIGHT_MAX=16` — одновременные изменяющие API-запросы;
- `API_HEAVY_INFLIGHT_MAX=4` — общий подлимит для
  `/api/tasks/complete` и `/api/admin/task/create`.

При отсутствии слота сервер отвечает `503 server_busy` и `Retry-After: 3`.
Клиент обязан повторить тот же запрос с прежним `operation_id`; существующие
проверки идемпотентности остаются источником истины. Персональные лимиты
`API_READS_PER_MIN` и `API_WRITES_PER_MIN` продолжают работать и отвечают `429`.

После допуска фото проходят отдельную ограниченную полосу:

- `MEDIA_NORMALIZE_CONCURRENCY=1` — одна активная Pillow-нормализация;
- `MEDIA_NORMALIZE_MAX_WAITERS=3` — не более трёх ожидающих работ;
- `MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC=5` — максимальное ожидание слота.

Переполнение или таймаут возвращает `503 media_processing_busy` с
`Retry-After`. Лимиты исходного файла 2,5 МБ, 20 мегапикселей, четырёх фото и
HTTP-запроса 16 МиБ не изменены.

## Telegram inbox и outbox

`/health/ready` теперь показывает глубину и возраст обеих очередей:

- `telegram_inbox_pending`, `telegram_inbox_oldest_at`,
  `telegram_inbox_backlogged`;
- `outbox_pending`, `outbox_oldest_at`, `outbox_backlogged`;
- soft/hard пороги и число webhook-отказов из-за перегрузки.

При глубине inbox от `TELEGRAM_INBOX_SOFT_LIMIT=100`, глубине outbox от
`TELEGRAM_OUTBOX_SOFT_LIMIT=100` или возрасте старейшего активного элемента
больше `TELEGRAM_QUEUE_OLDEST_SOFT_SEC=300` readiness становится нездоровым и
оператор получает сигнал мониторинга.

Новый уникальный update сначала проверяется на дубль. Если после этого в inbox
уже `TELEGRAM_INBOX_HARD_LIMIT=500` активных записей, webhook ничего не пишет и
отвечает `503` с `Retry-After: 2`. Telegram Bot API повторяет доставку webhook
при любом ответе вне диапазона 2xx, поэтому update остаётся у Telegram. Уже
сохранённый дубль даже при заполненной очереди получает `200`, что исключает
бесконечный повтор обработанного update.

Один обработчик ограничен `TELEGRAM_HANDLER_TIMEOUT_SEC=120` (допустимо
10–300 секунд). После deadline update не помечается выполненным: отменяемый
handler остаётся под своей lease до фактической остановки, затем запись уходит
в существующий retry/dead flow. Основной worker в это время продолжает брать
следующие доступные updates.

## Показатели readiness

Защищённый токеном `/health/ready` также отдаёт:

- `api_capacity.active_*` и накопительные `rejected_*`;
- `media_processing_capacity.active`, `waiters`, `rejected`;
- `webhook_overload_rejected`;
- монотонный `database_locked_errors` для API и критических workers.

Счётчики отказов накопительные с момента запуска процесса. Они нужны для
настройки порогов по результатам нагрузочного теста, а не для автоматического
увеличения лимитов.

## Обязательный тест перед расширением пилота

На копии production-конфигурации проверить одновременно минимум 100 первых
открытий Mini App, 50 заявок в минуту, 10 фотоотчётов по четыре фотографии и
20 Telegram updates в секунду. Условия прохождения: нет `database is locked`,
необработанных 500 и OOM; webhook p95 до 500 мс; память приложения ниже 600 МБ;
после окончания импульса обе очереди полностью опустошаются не более чем за
пять минут, а `database_locked_errors` остаётся равным нулю.

Этот gate автоматизирован в `scripts/pilot_load_test.py`. По умолчанию команда
только печатает план и ничего не меняет:

```bash
python scripts/pilot_load_test.py \
  --base-url https://staging.tasks.example.com
```

Применяемый запуск разрешён только против отдельного стенда с
`BIBITASKS_ENVIRONMENT=staging` и явным одноразовым
`PILOT_LOAD_TEST_ENABLED=true`. Он создаёт синтетические анкеты, задания,
фотографии и Telegram updates; после сохранения отчёта базу и media bucket
стенда нужно уничтожить целиком. Запуск против production запрещён проверкой
`/health/ready`.

### Одноразовый staging

Staging использует самостоятельный `compose.loadtest.yaml`: приложение, Caddy,
одноразовый load-runner и уникальные network/volumes с меткой
`com.bibitasks.purpose=loadtest`. Production Compose, network, volume, backup и
monitor в этот manifest не входят. Запускайте его из отдельного root-owned
checkout на выделенном Linux amd64 host; рабочая копия пользователя и общий с
production Docker project не допускаются preflight-проверкой.

На host нужен отдельный root-owned virtualenv только для bootstrap/preflight.
Сама нагрузка выполняется внутри неизменяемого application image:

`RELEASE_COMMIT`, `BIBITASKS_IMAGE`, домены и точные имена production
volume/network берутся из утверждённого release record и инвентаризации host.
До команд ниже задайте также путь к отдельному root-owned checkout:

```bash
REPO="/opt/bibitasks-release-${RELEASE_COMMIT}"
OPS_VENV=/opt/bibitasks-ops-venv
sudo python3 -m venv "$OPS_VENV"
sudo "$OPS_VENV/bin/pip" install --require-hashes \
  --requirement "$REPO/requirements.lock"
OPS_PY="$OPS_VENV/bin/python"
sudo docker pull "$BIBITASKS_IMAGE"
```

Подготовьте отдельного staging-бота и два разных root-owned token-файла режима
`0600`: staging и production. Production token используется только для live
`getMe` во время bootstrap и никогда не копируется в bundle. Генератор сравнит
неизменяемые numeric bot ID, проверит username staging-бота и создаст все
одноразовые имена и private evidence directory за пределами Git:

```bash
sudo "$OPS_PY" "$REPO/scripts/bootstrap_loadtest_env.py" \
  --apply \
  --domain "$LOAD_TEST_DOMAIN" \
  --confirm-domain "$LOAD_TEST_DOMAIN" \
  --production-domain "$PRODUCTION_DOMAIN" \
  --production-volume "$PRODUCTION_DATA_VOLUME" \
  --production-network "$PRODUCTION_NETWORK" \
  --release-commit "$RELEASE_COMMIT" \
  --image "$BIBITASKS_IMAGE" \
  --bot-token-file /run/secrets/bibitasks-staging-bot-token \
  --production-bot-token-file /run/secrets/bibitasks-production-bot-token \
  --bot-username BibiLoadTestBot \
  --admin-user-id 4400000000000000 \
  --output-dir "/etc/bibitasks-loadtest-${RELEASE_COMMIT}"
```

Admin ID намеренно синтетический и находится в верхней части 52-битного
диапазона Telegram. Не подставляйте личный Telegram ID: нагрузочный worker
добавит второй синтетический maker-checker автоматически.

До создания контейнеров выполните read-only preflight. Он требует чистый и
sealed checkout, точный commit/image, свежие project/network/три volume,
отдельные домен и bot ID, owner-only секреты, публичный DNS и точный Compose
render без production-ресурсов. `--apply` повторяет все проверки и только затем
в том же root-процессе запускает staging, закрывая промежуток между проверкой и
применением:

```bash
BUNDLE="/etc/bibitasks-loadtest-${RELEASE_COMMIT}"
sudo "$OPS_PY" "$REPO/scripts/loadtest_host_preflight.py" \
  --deploy-env "$BUNDLE/deploy.env" \
  --repo "$REPO" \
  --expected-commit "$RELEASE_COMMIT" \
  --expected-image "$BIBITASKS_IMAGE"

sudo "$OPS_PY" "$REPO/scripts/loadtest_host_preflight.py" \
  --apply \
  --confirm-domain "$LOAD_TEST_DOMAIN" \
  --deploy-env "$BUNDLE/deploy.env" \
  --repo "$REPO" \
  --expected-commit "$RELEASE_COMMIT" \
  --expected-image "$BIBITASKS_IMAGE"

curl --fail --show-error --silent \
  "https://${LOAD_TEST_DOMAIN}/health/live" >/dev/null
```

Public workload проходит через Caddy/TLS. Защищённый readiness остаётся закрыт
снаружи и читается runner только как `http://bibitasks:3000` внутри уникальной
Docker network. Секреты наследуются из staging env и не попадают в аргументы
процесса:

```bash
LOAD_TEST_ORIGIN="https://${LOAD_TEST_DOMAIN}"
sudo docker compose --env-file "$BUNDLE/deploy.env" \
  -f "$REPO/compose.loadtest.yaml" --profile loadtest \
  run --rm --no-deps loadtest-runner \
  python scripts/pilot_load_test.py \
  --apply \
  --base-url "$LOAD_TEST_ORIGIN" \
  --confirm-base-url "$LOAD_TEST_ORIGIN" \
  --health-base-url http://bibitasks:3000 \
  --secrets-from-environment \
  --admin-user-id 4400000000000000 \
  --report /evidence/pilot-load-report.json
```

Admin ID обязан совпадать со значением bootstrap. Webhook fixture — валидный
`poll_answer` без обработчика и исходящего Telegram-вызова; успешный gate
требует, чтобы все updates стали `done`, обе pending/dead очереди стали нулевыми,
а финальный свежий readiness остался зелёным. Исходящая доставка синтетических
outbox-сообщений заменена staging-only stub: durable queue и worker проходят
полный цикл, но Bot API не получает сообщения для несуществующих 52-битных ID.
Этот switch не запускается без одновременных `environment=staging` и
`PILOT_LOAD_TEST_ENABLED=true` и отдельно проверяется health gate.

Отчёт не содержит токены и синтетические user ID. Он считается успешным,
только если завершились все логические запросы, не было HTTP 500 или
`database is locked` (монотонный runtime-счётчик остался нулевым), webhook p95
не превысил 500 мс, RSS процесса остался ниже 600 МиБ, а inbox/outbox полностью
опустошились за пять минут.
Контролируемые `429/503` учитываются отдельно и повторяются тем же логическим
запросом; их наличие само по себе не подменяет успешное завершение сценария.

После сохранения отчёта сначала удалите webhook только у подтверждённого
staging-бота, затем уничтожьте именно проверенные load-test ресурсы. Cleanup не
сбрасывает pending Telegram updates и повторно сверяет numeric bot ID. Значения
ID/username берутся из не содержащего секретов `operator.json`:

```bash
STAGING_BOT_ID="$(sudo "$OPS_PY" -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["staging_bot_id"])' \
  "$BUNDLE/operator.json")"
STAGING_BOT_USERNAME="$(sudo "$OPS_PY" -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["staging_bot_username"])' \
  "$BUNDLE/operator.json")"

sudo "$OPS_PY" "$REPO/scripts/telegram_staging_cleanup.py" \
  --apply \
  --bot-token-file "$BUNDLE/bot-token" \
  --expected-bot-id "$STAGING_BOT_ID" \
  --expected-bot-username "$STAGING_BOT_USERNAME" \
  --confirm-bot-username "$STAGING_BOT_USERNAME"

sudo "$OPS_PY" "$REPO/scripts/loadtest_host_preflight.py" \
  --destroy \
  --confirm-domain "$LOAD_TEST_DOMAIN" \
  --deploy-env "$BUNDLE/deploy.env" \
  --repo "$REPO" \
  --expected-commit "$RELEASE_COMMIT" \
  --expected-image "$BIBITASKS_IMAGE"
```

`--destroy` проверяет labels/names и отсутствие production-ссылок до удаления,
а затем доказывает отсутствие network и всех трёх volumes. Evidence и secret
bundle автоматически не удаляются: сначала перенесите отчёт в утверждённое
хранилище, затем удалите точный каталог bundle по процедуре работы с секретами.

Этот capacity gate намеренно использует синтетические группы и отключённый join
admission, поэтому он не доказывает живую связь «production-группа — бот — Mini
App». Эту связь отдельно подтверждают `telegram_preflight.py`, управляемая
join-request ссылка, проверка прав/тем и публичный surface audit перед пилотом.

Перед несколькими экземплярами приложения необходим отдельный runtime на
PostgreSQL, private object storage и разделение API/inbox/outbox workers.
Существующий migration harness сам по себе этого не обеспечивает.
