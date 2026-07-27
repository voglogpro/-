# БибиЗадачи — технический запуск

Telegram-бот и Mini App для команды Бибибайка: участники берут задания в своём городе, прикладывают фото результата и получают бибибонусы на минуты поездки. Один бибибонус заменяет один рубль; базовая стоимость минуты задаётся через `RIDE_RUB_PER_MIN` и сейчас равна 8,5 ₽.

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

Целевая production-архитектура и план миграции описаны в [ADR-001](docs/ADR-001-professional-architecture.md). Telegram-настройка — в [чек-листе интеграции](docs/TELEGRAM-INTEGRATION.md).
Ежедневные действия и правила пилота — в [операционном регламенте](docs/PILOT-OPERATIONS.md).
Изолированная репетиция PostgreSQL — в [migration harness](docs/POSTGRES-MIGRATION-HARNESS.md); рабочий `main.py` пока остаётся SQLite-only.
Immutable image, backup cadence и безопасный rollback пилота — в [release/recovery runbook](docs/RELEASE-AND-RECOVERY.md).
