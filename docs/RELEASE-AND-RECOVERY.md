# Release и восстановление пилота

Этот runbook относится к **одному пилотному экземпляру SQLite**. Он не заменяет
PostgreSQL cutover из ADR-001 и не разрешает горизонтальное масштабирование.
Первый запуск выполняется единым stack из
[`OWNER-LAUNCH.md`](OWNER-LAUNCH.md) и `compose.pilot.yaml`. Старые раздельные
`compose.production.yaml`/`compose.backup.yaml` не используются для production:
они не содержат полного mount/preflight gate пилотного контура.

## Воспроизводимый образ

Тег `v*` запускает `.github/workflows/release.yml`. Сначала он обязан успешно
выполнить весь reusable quality workflow и только затем собирает образ из
зафиксированного base-image digest и hash-locked Python dependencies, публикует
его в GHCR, прикладывает SBOM/provenance и выводит неизменяемую ссылку вида:

```text
ghcr.io/<owner>/bibitasks@sha256:<digest>
```

В production разрешена только такая ссылка, не `latest` и не локальный `build`.
Контейнер сам проверяет digest до запуска приложения и backup scheduler:

```text
$env:BIBITASKS_IMAGE='ghcr.io/<owner>/bibitasks@sha256:<digest>'
$env:BIBITASKS_ENV_FILE='C:\secure\bibitasks.env'
docker compose -f compose.production.yaml pull
docker compose -f compose.production.yaml up -d
```

Файл секретов должен принадлежать только deployment-пользователю и не лежать в
Git/repository. После запуска `/health/ready` проверяется с `X-Health-Token`.
Остановка любого lifecycle/outbox/inbox worker теперь завершает основной процесс,
поэтому `restart: unless-stopped` восстанавливает его после рестарта Docker daemon, а не оставляет
полуживой контейнер.

## RPO backup

В owner stack backup уже работает отдельным изолированным сервисом. Для
обязательной pre-release копии разработчик временно останавливает scheduler,
создаёт одну ручную копию тем же immutable image и сразу возвращает scheduler:

```text
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml stop backup
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml run \
  --rm --no-deps backup python scripts/backup.py \
  --data-dir /app/data --output-dir /app/backups
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml start backup
```

Команда печатает `/app/backups/<snapshot>`; тот же `<snapshot>` находится на
host в `$BACKUP_DIR/<snapshot>`. Даже при ошибке ручной копии scheduler нужно
сразу вернуть и убедиться, что он healthy. Затем выбранный snapshot
восстанавливается в отсутствующий ранее `$EVIDENCE_DIR/restore-rehearsal`:

```text
docker run --rm --user 0:0 --read-only --tmpfs /tmp \
  -v "$BACKUP_SNAPSHOT:/backup:ro" -v "$EVIDENCE_DIR:/evidence" \
  "$BIBITASKS_IMAGE" python scripts/restore.py \
  --backup-dir /backup --restore-dir /evidence/restore-rehearsal
```

`BACKUP_SNAPSHOT` задаётся полным проверенным host-путём, без glob. В результате
появляются `manifest.json` и `restore-rehearsal/restore-report.json` для release
record.

`BACKUP_DIR` обязан указывать на зашифрованное off-host/NFS-хранилище, а не на
диск того же сервера. Backup service запускается только внутри `compose.pilot.yaml`
тем же immutable digest; отдельный `compose.backup.yaml` для пилота запрещён.

По умолчанию backup запускается каждые 600 секунд по start-to-start cadence —
время копирования не прибавляется к интервалу. Неудачная попытка повторяется не
позже чем через 60 секунд. Healthcheck краснеет сразу после неудачной попытки и
обязательно при возрасте последней успешной копии больше `BACKUP_RPO_SECONDS`
(по умолчанию 900 секунд). После трёх последовательных ошибок scheduler падает и
перезапускается. Retention и immutable/versioned policy задаются на стороне
backup storage — scheduler ничего не удаляет.

Не реже раза перед пилотом и затем по расписанию выполняется restore rehearsal
в **новый пустой** каталог:

```text
docker run --rm --read-only --tmpfs /tmp \
  -v /mnt/encrypted-bibibike-backups/<snapshot>:/backup:ro \
  -v /mnt/bibibike-restore-rehearsal:/restore \
  "$BIBITASKS_IMAGE" python scripts/restore.py \
  --backup-dir /backup --restore-dir /restore
```

Сверяются manifest/checksums, балансы, assignment, withdrawal, proof и последние
task/outbox записи. Каталог rehearsal нельзя монтировать как рабочий data volume.

## Обязательная запись promotion

Перед переключением трафика один релиз должен быть связан с одной проверенной
копией. Порядок нельзя переставлять:

1. Остановить writers или включить quiet window.
2. Создать backup и выполнить restore rehearsal в новый каталог.
3. Сохранить redacted JSON из `telegram_preflight.py` и `/health/ready`.
4. Авторизовать `gh` и registry на машине скаута. Для уже выпущенной v2.9.1
   `release_record.py` сохраняется как legacy evidence builder. Новые релизы
   проходят двухфазный [release gate v3](RELEASE-GATE-V3.md): candidate и
   rollback/recovery/live evidence. Подписанный final record появится только
   после отдельной реализации обязательного deployment enforcement; текущий
   validator всегда завершает процесс terminal NO-GO.
   Legacy `release_record.py` сам
   выполнит криптографическую проверку выбранных commit и digest командой:

```text
gh attestation verify oci://ghcr.io/voglogpro/bibitasks@sha256:<digest> \
  --repo voglogpro/- \
  --source-digest <full-40-char-sha> \
  --signer-workflow github.com/voglogpro/-/.github/workflows/release.yml \
  --deny-self-hosted-runners --format json
```

5. Следующий блок — только архивная процедура уже выпущенной v2.9.1. Он не
   является promotion authority для v2.10.0 и последующих версий. Два разных
   скаута создают локальную неперезаписываемую legacy-запись:

```text
python scripts/release_record.py \
  --commit acd0239a9ace9960c988c13e4608e2620b186fd3 \
  --image ghcr.io/voglogpro/bibitasks@sha256:472f78a2681795a114cfcaa9174c9cd11f03eef965de83becf4c06872d458cac \
  --schema-version 293 \
  --backup-manifest <backup>/manifest.json \
  --restore-report <restore>/restore-report.json \
  --preflight-report telegram-preflight.json \
  --readiness-report readiness.json \
  --repository voglogpro/- \
  --signer-workflow github.com/voglogpro/-/.github/workflows/release.yml \
  --approved-by <S1> --second-approved-by <S2> \
  --output <secure-release-dir>/release-record.json
```

Скрипт отказывается продолжать при mutable image, коротком commit SHA, одном
проверяющем, несовпавшем backup/restore digest, schema version, неподходящей
GitHub provenance, красном preflight или неготовом webhook/workers. Он записывает
только hashes и redacted сводку и не перезаписывает существующий путь. Это не WORM:
после создания hash записи обязательно закрепляется в append-only CI artifact или
внешнем versioned/WORM-хранилище. Для v2.10.0 создаётся candidate v1 со schema
`295` по [`RELEASE-GATE-V3.md`](RELEASE-GATE-V3.md). Даже полный набор evidence
сейчас заканчивается terminal NO-GO: deployment запрещён до реализации
криптографического quorum хранителей, одноразового challenge ledger и
контроллера, проверяющего подпись непосредственно перед переключением production.

## Однократный upgrade существующей v2.9.1 базы: recovery-key enrollment

Новый релиз сам создаёт recovery-key canary только в действительно пустом
`DATA_DIR`. Если рядом уже есть `bibitasks.db`, startup намеренно завершится с
ошибкой до открытия Telegram. Нельзя удалять canary, подставлять пустой volume
или временно переключать `BIBITASKS_ENVIRONMENT`: существующая база проходит
однократную явную церемонию `enroll-existing`.

Условия церемонии:

1. Зафиксировать immutable digest нового образа и остановить app и backup writer.
   Monitor можно оставить включённым; он должен поднять ожидаемый alert.
2. Убедиться, что в volume нет `bibitasks.db-wal`/`bibitasks.db-shm`. Если они
   остались, сначала запустить старый v2.9.1 образ, выполнить штатный checkpoint,
   снова остановить writers и начать церемонию заново.
3. Подготовить отдельную временную копию production env, доступную только UID
   приложения внутри контейнера. Не передавать ключи аргументами или через
   stdout:

   ```sh
   sudo install -d -o 10001 -g 10001 -m 0700 /run/bibitasks-key-enrollment
   sudo install -o 10001 -g 10001 -m 0400 \
     /etc/bibitasks/bibitasks.env /run/bibitasks-key-enrollment/keys.env
   sudo install -d -o 10001 -g 10001 -m 0700 /var/lib/bibitasks-enrollment
   ```

4. Получить SHA-256 остановленной базы из того же named volume. В выводе
   допустим только digest, он не является секретом:

   ```sh
   DB_SHA256="$(docker run --rm --network none --read-only \
     --user 10001:10001 --cap-drop ALL \
     --security-opt no-new-privileges:true \
     --mount type=volume,src="$BIBITASKS_DATA_VOLUME",dst=/app/data,readonly \
     --entrypoint python "$BIBITASKS_IMAGE" -c \
     "import hashlib; f=open('/app/data/bibitasks.db','rb'); print(hashlib.file_digest(f,'sha256').hexdigest())")"
   test "${#DB_SHA256}" -eq 64
   printf '%s\n' "$DB_SHA256"
   ```

5. Выполнить enrollment тем же immutable образом. Volume монтируется writable
   только для атомарного создания одного canary; сеть и все capabilities
   отключены:

   ```sh
   docker run --rm --network none --read-only --user 10001:10001 \
     --cap-drop ALL --security-opt no-new-privileges:true \
     --mount type=volume,src="$BIBITASKS_DATA_VOLUME",dst=/app/data \
     --mount type=bind,src=/run/bibitasks-key-enrollment/keys.env,dst=/run/keys.env,readonly \
     --mount type=bind,src=/var/lib/bibitasks-enrollment,dst=/evidence \
     --entrypoint python "$BIBITASKS_IMAGE" \
     scripts/recovery_key_canary.py enroll-existing \
     --data-dir /app/data \
     --confirm-database-sha256 "$DB_SHA256" \
     --env-file /run/keys.env \
     --report /evidence/recovery-key-enrollment.json
   ```

CLI — одноразовый bridge только от v2.9.1 schema `293` к следующему релизу
(destination schema `295` применяется уже при последующем startup). Он требует
точное подтверждение SHA-256, читает и инспектирует SQLite snapshot из байтов
одного стабильного regular-file descriptor без повторного открытия pathname,
проверяет `integrity_check=ok`, exact `user_version=293`, `DATA_DIR` mode `0700`
с правильным owner и link count `1`. Отсутствие WAL/SHM, неизменность DB inode,
байтов и pathname проверяются до и после публикации. CLI отклоняет активные
`pending`/`processing` строки без ciphertext, расшифровывает и сверяет fingerprint
всех сохранившихся Telegram/withdrawal ciphertext и сравнивает декодированный
32-byte material двух Fernet keys, а не только их текст. Он не читает ключи из
process environment и никогда не печатает ключи или plaintext. Успешный
stdout/report содержит только `database_sha256`, `canary_sha256`,
`schema_version`, `enrolled_at`; report создаётся exclusive с mode `0600` вне
репозитория. Если report запрошен, его target резервируется и fsync-ится до
проверки, полный report fsync-ится **до** атомарной публикации canary. Любая
последующая ошибка удаляет только exact inode report/canary, созданные этой
операцией; подменённый inode CLI никогда не удаляет.

6. Сверить оба digest в report с stdout, ограничить доступ к evidence и удалить
   временную копию env. Затем запустить новый stack, дождаться зелёного
   `/health/ready` и немедленно создать новый verified backup. Старые backups без
   canary/count contract не являются restore evidence для нового релиза.

Любая ошибка оставляет startup закрытым. Не повторять enrollment поверх уже
созданного canary: CLI обязан ответить отказом.

## Rollback

SQLite restore rehearsal/PIT staging через новый named volume выполняется
только скриптом `scripts/sqlite_volume_rollback.py` по пошаговой инструкции
[`SQLITE-VOLUME-ROLLBACK.md`](SQLITE-VOLUME-ROLLBACK.md). Он выполняет plan,
неразрушающий restore и повторную проверку реального volume. Production
activation fail-closed отключена до отдельной двусторонней сверки операций после
backup; ручное восстановление прямо в текущий volume запрещено.

Текущий runbook не разрешает production rollback ни вручную, ни скриптом.
Старый point-in-time backup не содержит операции ledger/withdrawals/payouts и
Telegram inbox/outbox после времени snapshot, поэтому его включение может
потерять или повторить финансовую операцию. `plan`, `apply` и `verify-stage`
служат только rehearsal/evidence и не меняют production deploy env.

Production rollback остаётся **NO-GO** до отдельного будущего процесса, который
машинно проверяет quiet-window final backup, двусторонний operation delta,
совместимость exact image/commit/schema, installed unit/runtime, отсутствие
replay/duplicate payouts, post-start readiness и проверенный backout. До этого
разрешено только восстановление сервиса вперёд исправляющим релизом без отката
данных.

Разрушительный Alembic downgrade baseline отключён. PostgreSQL rollback — только
restore проверенной pre-cutover базы либо отдельная forward corrective migration.
