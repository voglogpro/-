# PostgreSQL migration harness

Этот контур предназначен только для репетиции будущего перехода. Текущий
`main.py` по-прежнему работает только с SQLite и одним экземпляром процесса.
Наличие `MIGRATION_DATABASE_URL` не переключает runtime и не включает dual-write.

## Гарантии

- источник — только отдельная неизменяемая копия exact current SQLite schema;
- `quick_check`, полный manifest 28 таблиц/34 индексов и типизация всех строк
  выполняются до записи в PostgreSQL;
- target обязан быть отдельной пустой PostgreSQL database на точном Alembic head;
- import сохраняет legacy ID и выполняется одной транзакцией под advisory lock;
- Fernet ciphertext и Telegram payload переносятся opaque, без расшифровки;
- после загрузки identity sequences переводятся за максимальный сохранённый ID;
- независимый reconciler сравнивает counts и детерминированные typed digests,
  затем проверяет финансовые, assignment, withdrawal, inbox и media-инварианты;
- DSN, пароли, ciphertext и свободный пользовательский текст не попадают в отчёт.

## Локальная репетиция

```text
pip install -r requirements.txt -r requirements-migration.txt
docker compose -f compose.pg-test.yaml --profile migration up -d
$env:MIGRATION_DATABASE_URL='postgresql+psycopg://bibitasks_migrator:local-migration-only@127.0.0.1:55432/bibitasks_migration'
$env:MIGRATION_EXPECTED_DATABASE='bibitasks_migration'
$env:MIGRATION_EXPECTED_SCHEMA='public'
$env:MIGRATION_EXPECTED_SERVER_ADDRESS='127.0.0.1'
$env:MIGRATION_EXPECTED_SERVER_PORT='55432'
$env:MIGRATION_EXPECTED_USER='bibitasks_migrator'
alembic upgrade head
python tests/fixtures/build_current_sqlite.py --data-dir migration-source
python scripts/migrate_sqlite_to_postgres.py --source migration-source/bibitasks.db
python scripts/migrate_sqlite_to_postgres.py --source migration-source/bibitasks.db --apply --writers-stopped --database-url $env:MIGRATION_DATABASE_URL --expected-database bibitasks_migration --expected-schema public --expected-server-address 127.0.0.1 --expected-server-port 55432 --expected-user bibitasks_migrator --report migration-report.json
python scripts/reconcile_sqlite_postgres.py --source migration-source/bibitasks.db --database-url $env:MIGRATION_DATABASE_URL --expected-database bibitasks_migration --expected-schema public --expected-server-address 127.0.0.1 --expected-server-port 55432 --expected-user bibitasks_migrator --report reconciliation-report.json
```

Dry-run потоково читает и типизирует все строки, но не требует PostgreSQL. `apply` никогда
не создаёт database, не очищает непустой target и не имеет `--force`.
Полный source-schema fingerprint включает типы, nullability, defaults, PK и SQL
частичных индексов. Target дополнительно сверяется с canonical SQLAlchemy metadata;
database, schema, DSN endpoint address/port и role задаются явно. Внутренний
адрес PostgreSQL сохраняется только как диагностика. SQL-параметры скрыты даже
в случае ошибки драйвера.

Сам Alembic использует те же обязательные ожидания database/schema/host/port/user,
запрещает endpoint overrides и multi-host DSN, после соединения повторно проверяет
identity и берёт transaction advisory lock. Первая baseline-миграция допускается
только в пустую схему. Разрушительный `alembic downgrade base` намеренно отключён:
rollback выполняется восстановлением проверенной pre-cutover копии либо отдельной
forward-миграцией. Генерация offline SQL требует явного
`MIGRATION_OFFLINE_ACK=unverified-sql-generation-only`, потому что без соединения
нельзя доказать identity целевой базы.

## Cutover, которого этот контур ещё не разрешает

Перед настоящим переходом нужно остановить writers, дождаться остановки worker,
создать `scripts/backup.py`, проверить restore, выполнить import/reconcile и
только затем выпускать отдельную версию приложения с полностью перенесёнными
repository/transacton semantics. Частичный перенос одной таблицы, placeholder-
translator, fallback на SQLite и shadow/dual-write запрещены.
