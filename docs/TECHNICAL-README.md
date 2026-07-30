# БибиЗадачи — технический запуск

Текущий контракт данных: SQLite `300`; PostgreSQL/Alembic head
`0009_award_reversals`.
<!-- bibitasks-schema-contract: sqlite=300 alembic=0009_award_reversals -->

Telegram-бот и Mini App для команды Бибибайка: участники берут задания в своём городе, прикладывают фото результата и получают бибибонусы на минуты поездки. Один бибибонус заменяет один рубль; базовая стоимость минуты задаётся через `RIDE_RUB_PER_MIN` и сейчас равна 8,5 ₽.

Для первого production-пилота используйте единый пошаговый маршрут
[`OWNER-LAUNCH.md`](OWNER-LAUNCH.md). Он связывает VPS, автоматический HTTPS,
backup, реальные Telegram group/topic ID, профиль бота и live-приёмку.
Исполняемые сроки данных и ограничения Telegram/backup описаны в
[`PRIVACY-AND-RETENTION.md`](PRIVACY-AND-RETENTION.md).

## Что входит

- регистрация по имени, городу и навыкам без номера телефона;
- задания по городу, времени и исполнителю;
- фото задачи и фотоотчёт исполнителя;
- отказ исполнителя, отмена ответственным и автоматическое истечение срока;
- проверка результата ответственным;
- ссылка из карточки скаута на фактически доставленное OPS-объявление;
- журнал начислений и зашифрованные заявки на перевод бонусов;
- кассир выплаты, серверный lease countdown и безопасная передача следующей смене;
- долговечная очередь Telegram-уведомлений и обезличенная продуктовая воронка;
- связь с публичной группой `@bbbikefan`, ботом `@BbGalterbot`, Mini App
  `bibibike` и отдельной приватной OPS-supergroup для адресов и фотографий;
- светлая, тёмная и системная тема Telegram.

## Локальный запуск

1. Скопируйте `.env.example` в `.env`, задайте `BOT_TOKEN` и сгенерируйте
   независимые `WITHDRAW_ACCOUNT_KEY`, `TELEGRAM_INBOX_KEY` и
   `MEDIA_SIGNING_KEY` командами из комментариев в `.env.example`.
2. Установите Python 3.11+ и зафиксированные зависимости:
   `pip install --require-hashes -r requirements.lock`.
3. Запустите `python main.py`.
4. Проверьте `http://127.0.0.1:3000/health`.

Production secrets-файл создавайте вне репозитория. Токен передавайте генератору
только через переменную окружения — не аргументом командной строки:

```bash
export BOT_TOKEN='значение из BotFather'
python scripts/bootstrap_production_env.py \
  --output /etc/bibitasks/bibitasks.env \
  --public-base-url https://tasks.example.com \
  --privacy-url https://tasks.example.com/privacy \
  --privacy-controller-name 'Юридическое имя оператора' \
  --privacy-contact '@ответственный_за_данные' \
  --join-request-invite-url 'https://t.me/+ССЫЛКА_СОЗДАННАЯ_БОТОМ' \
  --group-id -1000000000001 \
  --ops-group-id -1000000000002 \
  --admin-id 111111111 --admin-id 222222222 \
  --webapp-shortname bibibike \
  --topic-news 11 --topic-chat 12 --topic-work 13 \
  --topic-franchise 14 --ops-topic-tasks 21
unset BOT_TOKEN
```

Генератор запрещает путь внутри Git, не перезаписывает существующий файл,
создаёт независимые webhook/media/encryption secrets и выставляет режим `0600`
на Linux. ID тем намеренно не имеют значений по умолчанию: возьмите реальные
`message_thread_id` из созданных Telegram-тем. HTTPS origin разрешён только на
стандартном порту 443. Production-генератор также требует публичную HTTPS-политику
со сроками хранения и порядком удаления. После настройки реального Telegram выполните read-only проверку,
которая не отправляет и не удаляет сообщения:

```bash
# Без BOT_TOKEN: проверяет то, что уже видит новый пользователь на t.me.
python scripts/telegram_public_surface_audit.py \
  --env-file /etc/bibitasks/bibitasks.env
python scripts/telegram_surface_setup.py --env-file /etc/bibitasks/bibitasks.env
# Просмотрите JSON-план. Только для подтверждённого @BbGalterbot:
python scripts/telegram_surface_setup.py --env-file /etc/bibitasks/bibitasks.env \
  --apply --confirm-bot @BbGalterbot
# Одноразово поставить проверенный JPG из репозитория как аватар бота:
python scripts/telegram_surface_setup.py --env-file /etc/bibitasks/bibitasks.env \
  --apply --confirm-bot @BbGalterbot --avatar-file logo.jpg
python scripts/telegram_preflight.py --env-file /etc/bibitasks/bibitasks.env
```

Surface setup идемпотентно приводит к release-конфигурации имя, полное и короткое
описание, команды и default menu button. По умолчанию это dry-run; он не меняет
webhook, группы, темы, сообщения, named Mini App или аватар. Явный
`--avatar-file` использует официальный `setMyProfilePhoto`, затем несколько раз
сверяет профиль. Одинаковый `file_unique_id` после подтверждённого API-ответа не
считается ошибкой: это может быть тот же файл или задержка видимости, поэтому
команду не нужно повторять. Named/Main Mini App настраивается в BotFather; live
acceptance сверяет итог в Telegram-клиенте.

Public-surface audit не меняет Telegram и не использует токен. Он проверяет
публичные preview-страницы группы и бота. Telegram сериализует в `Open App` даже
несуществующий `appname`, поэтому этот сигнал остаётся предупреждением и не
доказывает регистрацию/target URL named Mini App. Права, webhook, Main Mini App,
named Mini App и backend проверяются Bot API, BotFather и реальными клиентами.

Нулевой exit code означает, что обязательные проверки имени бота, групп,
прав, forum mode, webhook и Mini App menu button прошли. Затем выполните
[`LIVE-ACCEPTANCE.md`](LIVE-ACCEPTANCE.md) на реальных устройствах.

Для контейнера: `docker compose up --build`. Каталог `/app/data` обязательно должен находиться на постоянном диске. Текущая SQLite-сборка допускает только один экземпляр приложения.

Фотографии проходят через реестр `media_objects`: загрузка и проверка checksum
происходят до короткой бизнес-транзакции, а непривязанные объекты удаляются
с задержкой. Для одного экземпляра подходит `MEDIA_STORAGE=local`; для внешнего
private object storage задайте `MEDIA_STORAGE=s3`, bucket, region, HTTPS endpoint
при необходимости и стандартные AWS credentials. По умолчанию readiness требует
все четыре флага S3 Public Access Block. Для compatible storage без этого API
режим `operator_attested` разрешён только после отдельной проверки bucket policy/ACL
и явного `S3_PRIVATE_BUCKET_CONFIRMED=true`. Клиент получает только
короткоживущую proxy-ссылку по media ID, а не bucket/object key.

Проверенная копия создаётся командой
`python scripts/backup.py --data-dir data --output-dir backups --env-file .env`.
Восстановление выполняется только в новый каталог:
`python scripts/restore.py --backup-dir backups/<stamp> --restore-dir restored --env-file .env`.
S3-файлы реально скачиваются в backup; при restore загружаются в пустой target
bucket/prefix, сверяются по SHA-256, а новые VersionId записываются в копию БД.

Локально бот получает обновления через `TELEGRAM_UPDATE_MODE=polling`. В production
используйте `webhook`: задайте HTTPS-origin в `PUBLIC_BASE_URL`, две независимые
случайные строки `WEBHOOK_ROUTE_ID`, `WEBHOOK_SECRET`, `HEALTH_TOKEN` и отдельный
Fernet-ключ `TELEGRAM_INBOX_KEY`, затем перезапустите сервис.
Webhook сначала сохраняет update в `telegram_update_inbox`, отвечает Telegram и
только затем обрабатывает update отдельным worker с retry. Содержимое update
зашифровано; `drop_pending_updates=false` зафиксирован в коде. Readiness доступна
на `/health/ready` только с заголовком `X-Health-Token`, liveness — публично на
`/health/live`. Пример безопасного reverse proxy: `deploy/nginx.conf.example`.

## Безопасный запуск пилота

- Не храните `.env`, токен бота и базу в Git.
- Назначайте владельца через `ADMIN_IDS`; общий пароль администратора использовать нельзя.
- Назначьте минимум двух ответственных: создатель задания и исполнитель не могут сами подтвердить выплату.
- Не меняйте `WITHDRAW_ACCOUNT_KEY` без процедуры ротации: им зашифрованы ID
  аккаунтов в активных заявках. Полный ID показывается только ответственному и
  каждое открытие фиксируется в журнале.
- Бот должен быть администратором группы и иметь право публиковать сообщения в темах.
- Перед обновлением запускайте `scripts/backup.py`; простое копирование DB/WAL
  не является проверенной резервной копией.
- Проверьте восстановление копии до приглашения реальных участников.
- Проведите ручной smoke-тест на Android, iPhone и Telegram Desktop.
- Следите за защищённым `/health/ready`: `outbox_dead` и
  `telegram_inbox_dead` должны быть равны нулю, `telegram_receiver_ready`,
  `telegram_inbox_encryption_ready` и `withdrawal_encryption_ready` — `true`.
  Наличие dead update считается деградацией и требует ручного redrive, а старая
  необработанная очередь (`telegram_inbox_stale=true`) отключает readiness.

Целевая production-архитектура и план миграции описаны в [ADR-001](ADR-001-professional-architecture.md). Telegram-настройка — в [чек-листе интеграции](TELEGRAM-INTEGRATION.md), а безопасное создание и проверка ссылки на вступление — в [операторском регламенте](JOIN-REQUEST-OPERATIONS.md).
План безопасного удаления профиля находится в [IDENTITY-ERASURE-MIGRATION.md](IDENTITY-ERASURE-MIGRATION.md); self-service erasure пока не реализован и остаётся блокирующим gate.
Ежедневные действия и правила пилота — в [операционном регламенте](PILOT-OPERATIONS.md).
Изолированная репетиция PostgreSQL — в [migration harness](POSTGRES-MIGRATION-HARNESS.md); рабочий `main.py` пока остаётся SQLite-only.
Immutable image, backup cadence и безопасный rollback пилота — в [release/recovery runbook](RELEASE-AND-RECOVERY.md).
