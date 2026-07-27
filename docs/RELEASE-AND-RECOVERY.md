# Release и восстановление пилота

Этот runbook относится к **одному пилотному экземпляру SQLite**. Он не заменяет
PostgreSQL cutover из ADR-001 и не разрешает горизонтальное масштабирование.

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
поэтому `restart: always` действительно восстанавливает его, а не оставляет
полуживой контейнер.

## RPO backup

`BACKUP_DIR` обязан указывать на зашифрованное off-host/NFS-хранилище, а не на
диск того же сервера. Backup service запускается тем же immutable digest:

```text
$env:BACKUP_DIR='/mnt/encrypted-bibibike-backups'
$env:BIBITASKS_DATA_VOLUME='bibitasks_data'
docker compose -f compose.backup.yaml up -d
```

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

## Rollback

1. Остановить writers и сохранить диагностические логи.
2. Не запускать старый образ на уже изменённой SQLite-схеме.
3. Для code-only ошибки повторно поднять предыдущий digest только если его schema
   contract совпадает с текущим.
4. Для schema/data ошибки восстановить проверенную pre-release копию в новый
   volume, затем поднять предыдущий digest на этом новом volume.
5. Проверить `/health/ready`, dead queues и финансовую сверку до возврата трафика.

Разрушительный Alembic downgrade baseline отключён. PostgreSQL rollback — только
restore проверенной pre-cutover базы либо отдельная forward corrective migration.
