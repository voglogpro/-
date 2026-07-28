# Restore rehearsal и point-in-time staging SQLite-пилота

Этот сценарий восстанавливает утверждённую SQLite-копию в **новый Docker named
volume**. Текущий volume не монтируется на запись, не очищается и не удаляется.
Это **не команда production-активации**. Она подготавливает и проверяет
point-in-time копию на Ubuntu pilot-host из чистого repository worktree.

Скрипт поддерживает только локальные фотографии в SQLite-пилоте. Если backup
содержит готовые S3-объекты, он завершится до создания плана: откат S3 требует
отдельного пустого prefix/bucket и отдельной процедуры.

## Предварительные условия

- текущий `/etc/bibitasks/deploy.env` прошёл host preflight и имеет права `0600`;
- текущие image digest, полный commit и volume в deploy env соответствуют
  реально запущенному релизу;
- выбранный предыдущий `release-record.json` и его SHA-256 взяты из
  versioned/WORM evidence-хранилища, а не из сообщения или локальной копии;
- каталог backup называется ровно как `backup.id` в release record;
- target commit доступен в локальном Git;
- release image уже опубликован с OCI label
  `org.opencontainers.image.revision=<target commit>`;
- команду выполняет root/оператор с правом управлять Docker;
- `/run/lock` существует и недоступен для записи посторонним пользователям:
  все фазы используют один crash-safe `flock` на
  `/run/lock/bibitasks-sqlite-rollback.lock`.

В примерах все пути задаются явно, без glob и shell substitution из
непроверенных файлов:

```bash
sudo install -d -m 0700 /var/lib/bibitasks-rollback/incident-2026-07-28
sha256sum /srv/evidence/v2.9.1/release-record.json
```

Зафиксируйте напечатанный hash как `RECORD_SHA256` вручную. Не вычисляйте его
повторно внутри команды запуска: это уничтожило бы смысл независимой сверки.

## 1. Plan — только чтение и проверки

```bash
sudo python3 scripts/sqlite_volume_rollback.py plan \
  --deploy-env /etc/bibitasks/deploy.env \
  --repo /opt/bibitasks \
  --release-record /srv/evidence/v2.9.1/release-record.json \
  --release-record-sha256 <RECORD_SHA256_ИЗ_WORM> \
  --backup-dir /mnt/bibitasks-backups/<ТОЧНЫЙ_BACKUP_ID> \
  --output /var/lib/bibitasks-rollback/incident-2026-07-28/plan.json
```

Разрешение `apply` действует 30 минут. После этого plan нельзя использовать для
создания volume, но уже созданный volume можно сколько угодно раз проверять
read-only командой `verify-stage` с точными hash plan/stage. Plan проверяет оба точных image→commit соответствия,
legacy v2.9.1 release record, два approval, manifest/database hashes, schema, чистый Git,
наличие текущего volume и отсутствие нового. Имя нового volume генерируется со
случайным plan ID. Опциональный `--target-volume` допустим только для заранее
согласованного, ещё не существующего имени.

Для нового кандидата вместо legacy-флагов используются
`--release-candidate <path>` и `--release-candidate-sha256 <WORM_SHA256>`.
Candidate не разрешает deployment; он только связывает rehearsal с точным
software/deployment/backup subject.

Два ответственных отдельно читают `plan.json` и сверяют `current`, `target`,
`backup_id`, `manifest_sha256`, `database_sha256` и имя нового volume. Точная
строка для следующего шага находится в `apply_confirmation`.

## 2. Apply — restore rehearsal в новый volume

```bash
sudo python3 scripts/sqlite_volume_rollback.py apply \
  --plan /var/lib/bibitasks-rollback/incident-2026-07-28/plan.json \
  --confirm 'APPLY <PLAN_ID> TO <40_CHAR_TARGET_COMMIT> ON <NEW_VOLUME>' \
  --stage-report /var/lib/bibitasks-rollback/incident-2026-07-28/stage-report.json
```

`apply` ещё не останавливает production и не меняет deploy env. Он:

1. повторно сверяет неизменность plan, release record, backup, Git и deploy env;
2. создаёт отсутствующий volume с labels plan/commit/image/manifest;
3. запускает target image без сети, read-only и без capabilities;
4. восстанавливает данные во внутренний staging-каталог volume;
5. сверяет `restore-report.json`, `integrity_check`, schema и оба SHA-256;
6. получает реальный непривилегированный UID/GID из target image; только
   короткая promotion-команда получает единственную capability `CAP_CHOWN`,
   обрабатывает дерево от листьев к корню и назначает права `0700/0600`;
7. повторяет DB/hash/ownership/readability-проверку уже target UID/GID;
8. создаёт `stage-report.json` с exact labels, fingerprint volume,
   `ready_for_point_in_time_recovery_review: true` и обязательным
   `production_activation_enabled: false`.

При любой ошибке новый volume остаётся изолированным для расследования. Скрипт
никогда не выполняет `docker volume rm`. Не пытайтесь повторно использовать
частично созданный volume: создайте новый plan с новым именем.

Для анализа обязательна ручная сверка бизнес-инвариантов из восстановленной
БД: балансы, назначения, заявки на перевод, доказательства, dead queues и
последние task/outbox/inbox записи. Для чтения используйте target image и mount
`<NEW_VOLUME>:/app/data:ro`; не запускайте второй экземпляр бота или webhook.

## 3. Повторная проверка реального volume

Перед сохранением evidence перечитайте сам volume, а не только старый отчёт:

```bash
sha256sum /var/lib/bibitasks-rollback/incident-2026-07-28/stage-report.json
sudo python3 scripts/sqlite_volume_rollback.py verify-stage \
  --plan /var/lib/bibitasks-rollback/incident-2026-07-28/plan.json \
  --stage-report /var/lib/bibitasks-rollback/incident-2026-07-28/stage-report.json \
  --stage-report-sha256 <STAGE_REPORT_SHA256_ИЗ_EVIDENCE> \
  --output /var/lib/bibitasks-rollback/incident-2026-07-28/verify-report.json
```

Проверка требует полного совпадения **всех** labels и fingerprint Docker volume,
после чего повторно читает `restore-report.json`, SQLite integrity/schema/hash,
размер и SHA-256 каждого local media из manifest, отсутствие лишних media,
владельцев и доступность файлов target UID/GID. Удалённый и заново созданный
volume с теми же данными будет отклонён по fingerprint.

`verify-report.json` создаётся только один раз с правами `0600` вне Git и не
перезаписывается. В нём нет локальных путей: только version/time, hash plan и
stage-report, текущие и целевые commit/image/volume, итоговая проверка данных и
явное `production_activation_enabled: false`. Текущий production volume
проверяется только на точное наличие и никогда не монтируется.

## Production activation: NO-GO

Скрипт не предоставляет CLI-команду activation и не создаёт `deploy.next.env`.
Compatibility API `activate_plan` всегда повторно проверяет реальный staged
volume и затем fail-closed завершается **до** `systemctl`, Git checkout или
изменения deploy env.

Причина: backup старого релиза — point-in-time снимок. После него могли появиться
ledger entries, выплаты, withdrawals и обработанные inbox/outbox updates.
Автоматическое включение старого снимка способно повторить операцию либо потерять
начисление. Одной ручной проверки старой БД для production недостаточно.

Production recovery остаётся NO-GO, пока отдельный проверенный процесс не
докажет одновременно:

- quiet window и финальный backup **текущего** состояния;
- двусторонний delta ledger/withdrawals/payouts/inbox/outbox между финальным
  состоянием и recovery point;
- отсутствие повторной выплаты и replay Telegram updates;
- exact installed systemd unit, host runtime, commit/image/schema binding;
- post-start readiness, dead queues и финансовую сверку;
- независимое одобрение двух ответственных и исполняемый recovery-backout.

Для code-only rollback допустима отдельная процедура с финальным backup текущей
БД в новом volume только при машинно доказанной совместимости exact schema и
данных со старым image. Текущий скрипт намеренно этого не утверждает и production
не переключает.

## Что является доказательством rehearsal

Сохраните вне Git в закрытом append-only/versioned хранилище:

- `plan.json` и его SHA-256;
- `stage-report.json` и SHA-256;
- target release record и его исходный WORM hash;
- backup `manifest.json`;
- неперезаписываемый `verify-report.json` из свежего `verify-stage`;
- результаты ручной PIT-сверки;
- подписи двух разных ответственных и время начала/окончания окна.

Для регулярной репетиции выполняются `plan` + `apply` + `verify-stage`:
production всегда остаётся на текущем commit/image/volume. Активация допускается
только будущим отдельным процессом, закрывающим перечисленные выше gates, либо
на полностью изолированном staging host с отдельными ботом, доменом и группами.
