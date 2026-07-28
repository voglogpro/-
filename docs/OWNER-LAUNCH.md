# Запуск БибиЗадач: маршрут для владельца

Это единственный поддерживаемый маршрут пилота: **один Ubuntu 24.04 VPS + Docker
Compose + Caddy + SQLite на постоянном диске + зашифрованная копия вне VPS**.
Caddy сам получает и обновляет HTTPS-сертификат. Один экземпляр приложения и
backup-процесс используют общий Docker volume; запуск второй реплики запрещён.

Не запускайте пилот прямо из рабочей папки Windows и не покупайте PaaS до
перехода runtime на PostgreSQL/object storage. Текущая схема резервирования
рассчитана на общий локальный volume приложения и отдельного backup-контейнера.

## Кто за что отвечает

| Роль | Ответственность |
|---|---|
| Владелец | VPS, домен, BotFather/Main/Named Mini App, скрытый ввод token, четыре тестовых аккаунта, LIVE PASS/FAIL |
| Разработчик | release tag/digest, установка, inventory ID, evidence pack, диагностика и staging failure drill |
| S1 и S2 | два разных ответственных: маркеры ID, права групп, выплаты, cleanup и подпись приёмки |
| U1 и U2 | два обычных тестовых участника на разных устройствах |

Токен бота не отправляется в чат, issue, Git или командный аргумент. Владелец
вводит его только в скрытый prompt на доверенной машине; после генерации он
хранится в `/etc/bibitasks/bibitasks.env` с режимом `0600`.

## 1. Что подготовить до сервера

- VPS: x86_64/amd64, 2 vCPU, 4 GB RAM, 40 GB SSD; Ubuntu 24.04 LTS; отдельный
  пользователь с `sudo`. Открыты только SSH, TCP 80 и TCP/UDP 443.
- Поддомен, например `tasks.example.com`, с A-записью на IPv4 VPS. AAAA
  добавляйте только если IPv6 действительно настроен.
- Зашифрованное off-host/NFS-хранилище, смонтированное, например, в
  `/mnt/bibitasks-backups`. Каталог на том же диске VPS не считается копией.
- Публичная forum-supergroup `@bbbikefan` с названием «Бибибайк | Сообщество помощников» и темами
  «Новости», «Болталка», «Работа», «Франшиза».
- Отдельная private forum-supergroup без username с темой «Задания OPS».
- `@BbGalterbot` добавлен администратором в обе группы с минимально нужными
  правами на чтение/отправку/удаление сообщений и работу в темах.
- Минимум два Telegram user ID ответственных и четыре тестовых аккаунта:
  S1 Desktop, S2 iPhone, U1 Android, U2 iOS/Desktop.

Заполните инвентарную таблицу; значение считается готовым только после проверки
в реальном чате/теме.

| Переменная | Откуда взять | Проверено |
|---|---|---|
| `BIBITASKS_DOMAIN` | подготовленный поддомен без `https://` | ☐ |
| `PRIVACY_URL` | публичная HTTPS-политика со сроками хранения и порядком удаления | ☐ |
| `PRIVACY_CONTROLLER_NAME` | точное юридическое/публичное имя оператора пилота | ☐ |
| `PRIVACY_CONTACT` | рабочий контакт запросов на исправление и удаление | ☐ |
| `GROUP_ID` | `chat_id` публичной `@bbbikefan` | ☐ |
| `TOPIC_NEWS` | `message_thread_id` темы «Новости» | ☐ |
| `TOPIC_CHAT` | `message_thread_id` темы «Болталка» | ☐ |
| `TOPIC_WORK` | `message_thread_id` темы «Работа» | ☐ |
| `TOPIC_FRANCHISE` | `message_thread_id` темы «Франшиза» | ☐ |
| `OPS_GROUP_ID` | `chat_id` отдельной private OPS-группы | ☐ |
| `OPS_TOPIC_TASKS` | `message_thread_id` темы «Задания OPS» | ☐ |
| `ADMIN_IDS` | Telegram user ID S1 и S2 | ☐ |

Ещё до получения токена можно проверить публично видимый маршрут. Команда
ничего не меняет и завершается ошибкой при старом названии или описании.
Публичный `Open App` не доказывает регистрацию named Mini App — это отдельный
BotFather/live-client gate:

```bash
python scripts/telegram_public_surface_audit.py --env-file .env.example
```

## 2. Подготовка VPS

Разработчик устанавливает Docker Engine и Compose plugin по актуальной
[официальной инструкции Docker для Ubuntu](https://docs.docker.com/engine/install/ubuntu/),
без `curl | sh`, затем проверяет:

```bash
docker version
docker compose version --short
```

Используйте актуальный Compose plugin из официального Docker repository. На
production VPS не должно быть сохранённого входа в чужой registry.

Репозиторий клонируется в `/opt/bibitasks`, checkout фиксируется на полном commit
SHA проверенного релиза. Каталоги секретов и внешней копии проверяются явно:

```bash
sudo install -d -m 0700 /etc/bibitasks
sudo test -d /mnt/bibitasks-backups
findmnt --target /mnt/bibitasks-backups
echo bibitasks-offhost-v1 | sudo tee /mnt/bibitasks-backups/.bibitasks-offhost >/dev/null
sudo chmod 0400 /mnt/bibitasks-backups/.bibitasks-offhost
```

Если `findmnt` показывает диск самого VPS либо ничего — STOP, backup gate не
пройден. До первого запуска `dig +short tasks.example.com` должен вернуть IP VPS.

## 3. Release image и production environment

Последний проверенный baseline (не текущий deploy candidate):

```text
tag: v2.9.1
commit: acd0239a9ace9960c988c13e4608e2620b186fd3
image: ghcr.io/voglogpro/bibitasks@sha256:472f78a2681795a114cfcaa9174c9cd11f03eef965de83becf4c06872d458cac
platform: linux/amd64
```

28 июля 2026 года tag `2.9.1` проверен через anonymous registry pull: OCI index
указывает на commit выше и amd64 manifest `sha256:e36bd41bf130dcb2c1ed681767fa92ad13ba9c125aadbc651b28af33d865841a`.
Release workflow публикует SPDX SBOM и SLSA provenance.

Этот образ подтверждает только прежний baseline. Он не содержит новых scripts и
флагов candidate v1, monitor/recovery и schema 296. Поэтому приведённый digest
нельзя использовать для запуска текущего worktree v2.10.0. До публикации и
проверки нового immutable image действует terminal **NO-GO**.

Разработчик создаёт подписанный release tag только из зелёного commit. GitHub
Actions публикует неизменяемую ссылку вида
`ghcr.io/voglogpro/bibitasks@sha256:<64 hex>`. Значения `latest`, простой тег и
локальный `build` запрещены:

В GitHub Packages владелец/разработчик делает пакет `bibitasks` публичным и
проверяет anonymous pull на чистом VPS. Это выбранный контракт пилота: registry
token на сервере не хранится. `unauthorized` — STOP; не используйте личный PAT
с широкими правами как временный обход. Следующий блок — шаблон для будущего
v2.10.0 digest. **Не выполнять**, пока CI и независимая проверка не заменят оба
placeholder точными значениями нового релиза:

```bash
export BIBITASKS_IMAGE='<NEW_VERIFIED_V2_10_0_IMMUTABLE_IMAGE_REQUIRED>'
docker pull "$BIBITASKS_IMAGE"
BIBITASKS_UID="$(docker run --rm --entrypoint id "$BIBITASKS_IMAGE" -u)"
BIBITASKS_GID="$(docker run --rm --entrypoint id "$BIBITASKS_IMAGE" -g)"
sudo chown "$BIBITASKS_UID:$BIBITASKS_GID" /mnt/bibitasks-backups
sudo chmod 0700 /mnt/bibitasks-backups
```

Если off-host storage запрещает такой `chown`, оператор заранее выдаёт этому
UID/GID право записи средствами хранилища; ослаблять каталог до `0777` нельзя.

### Получить настоящие ID без сторонних ботов

Это делается после `docker pull`, но **до первого запуска polling/webhook**. S1
отправляет по одной точной команде в соответствующую тему:

| Где | Маркер | Результат |
|---|---|---|
| Новости | `/inventory_news@BbGalterbot` | `TOPIC_NEWS` |
| Болталка | `/inventory_chat@BbGalterbot` | `TOPIC_CHAT` |
| Работа | `/inventory_work@BbGalterbot` | `TOPIC_WORK` |
| Франшиза | `/inventory_franchise@BbGalterbot` | `TOPIC_FRANCHISE` |
| Private OPS / Задания OPS | `/inventory_ops_tasks@BbGalterbot` | `OPS_TOPIC_TASKS` |

Кроме того, S1 и S2 каждый отправляют боту **в личный чат**
`/inventory_admin@BbGalterbot`. Затем разработчик запускает:

```bash
read -rsp 'BOT_TOKEN: ' BOT_TOKEN; echo
export BOT_TOKEN
docker run --rm -e BOT_TOKEN -e BOT_USERNAME=BbGalterbot \
  "$BIBITASKS_IMAGE" python scripts/telegram_inventory.py --include-admin-ids
unset BOT_TOKEN
```

Скрипт вызывает только `getMe`, `getWebhookInfo` и `getUpdates`, не подтверждает
updates и не показывает текст, имя автора или token. Только при явном флаге он
выводит ID двух людей, приславших точный private-маркер. Он откажется работать,
если token принадлежит другому боту, webhook включён или обязательных маркеров
не хватает. При 100+ ожидающих updates он также остановится: backlog разбирает
разработчик отдельным контролируемым cutover, владелец его не сбрасывает.
Перенесите ID в таблицу, сохраните вывод как закрытое доказательство и удалите
маркеры вручную.

Production-файл генерируется контейнером, поэтому секреты не попадают в shell
history. Ниже заменяются только домен и реальные ID из таблицы:

```bash
read -rsp 'BOT_TOKEN: ' BOT_TOKEN; echo
read -rsp 'MONITOR_ALERT_BOT_TOKEN (отдельный бот): ' MONITOR_ALERT_BOT_TOKEN; echo
export BOT_TOKEN MONITOR_ALERT_BOT_TOKEN
docker run --rm --user 0:0 -e BOT_TOKEN \
  -e MONITOR_ALERT_BOT_TOKEN \
  -v /etc/bibitasks:/secure "$BIBITASKS_IMAGE" \
  python scripts/bootstrap_production_env.py \
  --output /secure/bibitasks.env \
  --monitor-secrets-dir /secure \
  --public-base-url https://tasks.example.com \
  --privacy-url https://tasks.example.com/privacy \
  --privacy-controller-name 'Юридическое имя оператора' \
  --privacy-contact '@ответственный_за_данные' \
  --group-id -1000000000001 --ops-group-id -1000000000002 \
  --admin-id 111111111 --admin-id 222222222 \
  --webapp-shortname bibibike \
  --topic-news 11 --topic-chat 12 --topic-work 13 \
  --topic-franchise 14 --ops-topic-tasks 21
unset BOT_TOKEN MONITOR_ALERT_BOT_TOKEN
sudo chmod 0600 /etc/bibitasks/bibitasks.env \
  /etc/bibitasks/monitor-alert-bot-token \
  /etc/bibitasks/monitor-health-token
```

Создайте `/etc/bibitasks/deploy.env` без token:

```dotenv
BIBITASKS_IMAGE=<NEW_VERIFIED_V2_10_0_IMMUTABLE_IMAGE_REQUIRED>
BIBITASKS_RELEASE_COMMIT=<NEW_VERIFIED_V2_10_0_COMMIT_REQUIRED>
BIBITASKS_ENV_FILE=/etc/bibitasks/bibitasks.env
BIBITASKS_DOMAIN=tasks.example.com
BACKUP_DIR=/mnt/bibitasks-backups
BACKUP_SENTINEL=/mnt/bibitasks-backups/.bibitasks-offhost
BACKUP_SENTINEL_VALUE=bibitasks-offhost-v1
BACKUP_EXPECTED_SOURCE=backup.example.com:/exports/bibitasks
BIBITASKS_DATA_VOLUME=bibitasks_data
MONITOR_ALERT_BOT_TOKEN_FILE=/etc/bibitasks/monitor-alert-bot-token
MONITOR_HEALTH_TOKEN_FILE=/etc/bibitasks/monitor-health-token
MONITOR_ALERT_CHAT_ID=-1000000000003
MONITOR_INSTANCE_LABEL=pilot-1
```

`MONITOR_ALERT_BOT_TOKEN` должен принадлежать отдельному боту только для
аварийных сообщений. Добавьте его в приватную alert-группу
`MONITOR_ALERT_CHAT_ID` с правом отправки сообщений, но без прав администратора.
Не используйте для мониторинга основной bot token.

## 4. Запуск и автоматический HTTPS

**Terminal STOP:** этот раздел остаётся справочным и не выполняется, пока
placeholder в `deploy.env` не заменены новым проверенным commit/digest и release
gate не получил отдельный криптографически принудительный deployment controller.

Из `/opt/bibitasks` на зафиксированном commit:

```bash
export RELEASE_SHA="$(git rev-parse HEAD)"
export EVIDENCE_DIR="/var/lib/bibitasks-release/$RELEASE_SHA"
sudo install -d -o "$(id -u)" -g "$(id -g)" -m 0700 "$EVIDENCE_DIR"
docker compose --env-file /etc/bibitasks/deploy.env \
  -f compose.pilot.yaml config --quiet
docker compose --env-file /etc/bibitasks/deploy.env \
  -f compose.pilot.yaml pull
sudo python3 scripts/pilot_host_preflight.py \
  --deploy-env /etc/bibitasks/deploy.env --repo /opt/bibitasks \
  --expected-commit "$RELEASE_SHA" --expected-image "$BIBITASKS_IMAGE" \
  > "$EVIDENCE_DIR/host-preflight.json"
sudo install -m 0644 deploy/bibitasks-pilot.service.example \
  /etc/systemd/system/bibitasks-pilot.service
sudo systemctl daemon-reload
sudo systemctl enable --now bibitasks-pilot.service
docker compose --env-file /etc/bibitasks/deploy.env \
  -f compose.pilot.yaml ps
curl --fail --show-error https://tasks.example.com/health/live
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml \
  exec -T --user 10001:10001 monitor python scripts/pilot_monitor.py --test-alert
```

В приватной alert-группе должно появиться тестовое сообщение. Подробный runbook,
порог срабатывания и проверка recovery описаны в
[`PILOT-MONITORING.md`](PILOT-MONITORING.md).

Публичный `/health/ready` специально возвращает `404`: он содержит внутренние
признаки и защищён token. Полная проверка выполняется внутри контейнера:

```bash
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml \
  exec -T bibitasks python -c "import json,os,urllib.request; q=urllib.request.Request('http://127.0.0.1:3000/health/ready',headers={'X-Health-Token':os.environ['HEALTH_TOKEN']}); print(urllib.request.urlopen(q,timeout=5).read().decode())" \
  > "$EVIDENCE_DIR/readiness.json"
python -m json.tool "$EVIDENCE_DIR/readiness.json"
```

PASS: оба app/backup healthy, Caddy запущен, live=`200`, readiness=`200`, dead
queues равны нулю, receiver и encryption readiness равны `true`.

## 5. Связать Telegram с HTTPS-приложением

Сначала dry-run, затем одно явное применение к проверенному username:

```bash
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml \
  run --rm --no-deps bibitasks python scripts/telegram_public_surface_audit.py \
  > "$EVIDENCE_DIR/telegram-public-surface-before.json"
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml \
  run --rm --no-deps bibitasks python scripts/telegram_surface_setup.py
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml \
  run --rm --no-deps bibitasks python scripts/telegram_surface_setup.py \
  --apply --confirm-bot @BbGalterbot --avatar-file logo.jpg
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml \
  run --rm --no-deps bibitasks python scripts/telegram_public_surface_audit.py \
  > "$EVIDENCE_DIR/telegram-public-surface-after.json"
```

Автоматический setup меняет профиль, команды и кнопку меню, но не создаёт Main
или Named Mini App. Владелец бота открывает `@BotFather` → `/mybots` →
`@BbGalterbot` → **Bot Settings** → **Configure Mini App**:

1. Включает/редактирует **Main Mini App** и задаёт точный URL
   `https://tasks.example.com/`.
2. В списке Mini Apps открывает существующий short name `bibibike`; если его
   нет — создаёт по подсказкам BotFather. URL тот же production origin.
3. Проверяет `https://t.me/BbGalterbot/bibibike` и кнопку **Open App** профиля
   на двух Telegram-клиентах. Они должны открыть именно production-приложение.
4. Проверяет кнопку меню «Открыть задания» — её URL уже выставил setup-скрипт.

Официально Telegram называет Main Mini App и menu button разными поверхностями;
настройка Main выполняется через BotFather. После ручной настройки:

```bash
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml \
  run --rm --no-deps bibitasks python scripts/telegram_preflight.py \
  > "$EVIDENCE_DIR/telegram-preflight.json"
```

Preflight должен завершиться exit code `0` и JSON `"ok": true`. Бот должен
называться «БибиЗадачи · Бибибайк» с зелёным логотипом; OPS должна оставаться
private.

## 6. Evidence pack разработчика

До live-приёмки разработчик передаёт владельцу закрытый пакет в
`$EVIDENCE_DIR`: полный commit/digest, ссылки на зелёный CI, результат GitHub
attestation, `readiness.json`, `telegram-preflight.json`, backup `manifest.json`,
`restore-report.json` из восстановления в **новый пустой каталог** и
`release-candidate.json`. Команды и fail-closed
проверки даны в [`RELEASE-AND-RECOVERY.md`](RELEASE-AND-RECOVERY.md). Пакет не
коммитится; его hash закрепляется во внешнем versioned/append-only хранилище.

Блок 9 выполняется разработчиком на отдельном staging domain, отдельном боте и
отдельных Telegram-группах. Подписанный staging-отчёт входит в evidence pack;
владелец не меняет retry/права production. Без любого из этих артефактов — NO-GO.

## 7. Результат live-приёмки

Заполните [`LIVE-ACCEPTANCE-REPORT.template.md`](LIVE-ACCEPTANCE-REPORT.template.md)
по инструкции [`LIVE-ACCEPTANCE.md`](LIVE-ACCEPTANCE.md). Блок отказоустойчивости
на staging проводит разработчик, не владелец на production. Девять PASS,
подписи S1/S2, проверенная restore-копия и отсутствие критических дефектов дают
только **LIVE PASS**. Это не разрешение на deployment и не отменяет terminal
NO-GO release gate v3. Отчёт со скриншотами и operational ID хранится вне
публичного Git.
