# Offline recovery-key evidence

`scripts/secret_recovery_evidence.py` создает fail-closed доказательство того,
что ключи из disaster-recovery bundle соответствуют конкретной резервной копии
и восстановленной SQLite-базе. Это обязательная проверка до production
activation, но она сама по себе не переключает volume и не заменяет release
record.

## Что действительно проверяется

- открываются ровно две физически разные копии encrypted age bundle;
- обе копии полностью хешируются через стабильные file descriptors и должны
  иметь одинаковые размер и SHA-256;
- обе копии независимо расшифровываются в память фиксированным бинарником
  `/usr/local/bin/age`; plaintext env никогда не записывается на диск;
- `TELEGRAM_INBOX_KEY` и `WITHDRAW_ACCOUNT_KEY` должны быть разными валидными
  Fernet-ключами и расшифровать сохраненный **до аварии**
  `recovery-key-canaries.json`;
- manifest связывает SHA/bytes/schema базы, canary и ожидаемые encrypted-row
  counts; restore report связывает тот же manifest, DB и canary;
- все ненулевые Telegram/withdrawal ciphertext расшифровываются;
- Telegram `update_id` сверяется с ключом строки, а `payload_sha256` — с точным
  application HMAC; `account_fingerprint` выплаты также проверяется точно;
- активные `pending`/`processing` строки с `NULL` ciphertext запрещены;
- Telegram preflight, readiness и monitor canary обязательны, должны быть не
  старше 24 часов и не более чем на 5 минут из будущего; readiness обязан
  совпадать с точной release version;
- monitor drill должен подтвердить доставленные incident и recovery как минимум
  для `application` и `backup`, а `dead_queues` также должен быть healthy.

Самостоятельный encrypt/decrypt challenge намеренно не используется: случайный
ключ способен пройти такую самопроверку. Ключ доказывается только ранее
созданным persistent canary.

## Что отчет не доказывает

Две одинаковые копии доказывают наличие двух переданных файлов, но не
криптографический quorum хранителей. В отчете это честно отмечено как
`operator_assertions.custodian_quorum_cryptographically_verified=false`.
Имена/ID хранителей и хранилищ не принимаются и не хешируются как security proof.

Отчет не содержит plaintext/ciphertext, record IDs, значений или hashes ключей,
identity contents, локальных путей и stderr `age`. В нем остаются только safe
counts, booleans, timestamps и cryptographic digests. Output создается
эксклюзивно (`O_EXCL`) с режимом `0600`, не перезаписывается и запрещен внутри
Git checkout.

## Обязательный preflight recovery-host

Церемония выполняется на Linux с `/proc`, полностью отключенной сетью и заранее
установленным `age`. Tool принимает только этот путь:

```text
/usr/local/bin/age
```

Файл должен быть обычным executable, не symlink, принадлежать root и не быть
доступным для записи group/other. Перед церемонией независимо сверьте checksum
бинарника с утвержденным offline inventory и проверьте интерфейс:

```bash
test -x /usr/local/bin/age
/usr/local/bin/age --version
stat -c 'owner=%u mode=%a' /usr/local/bin/age
```

Если файла нет, owner не `0`, mode допускает group/other write или интерфейс
`--decrypt --identity` не поддерживается — **остановитесь**. Не скачивайте и не
обновляйте инструмент во время recovery ceremony.

В обычном Windows dev-окружении этого trusted path нет; production subprocess
там намеренно не запускается. Unit tests используют injected synthetic runner,
но production acceptance требует отдельной Linux rehearsal с реальным `age`.

## Входные файлы

Все paths задаются явно, каждый input обязан быть regular non-symlink file:

1. две encrypted bundle copies с разных off-host носителей;
2. private age identity (`0600`);
3. восстановленный `bibitasks.db`;
4. восстановленный `recovery-key-canaries.json`;
5. исходный `manifest.json` и созданный restore `restore-report.json`;
6. fresh Telegram preflight, readiness и monitor drill JSON.

Manifest обязан содержать:

```json
{
  "database": {
    "path": "bibitasks.db",
    "bytes": 123,
    "sha256": "<64 hex>",
    "integrity_check": "ok",
    "schema_version": 293,
    "telegram_ciphertext_count": 0,
    "telegram_active_null_count": 0,
    "withdrawal_ciphertext_count": 0,
    "withdrawal_active_null_count": 0
  },
  "recovery_key_canary": {
    "path": "recovery-key-canaries.json",
    "bytes": 123,
    "sha256": "<64 hex>"
  }
}
```

Canary имеет exact canonical schema version 1, domain
`bibitasks.recovery-key-canary`, purpose `pre-disaster-key-binding`, 32-byte
base64url nonce и два Fernet ciphertext с ролями `telegram-inbox` и
`withdraw-account`. Canary создается приложением до аварии, входит в backup и
копируется restore-процедурой; создавать новый canary после потери ключей нельзя.

Каждый live JSON должен иметь top-level `generated_at` в UTC. Текущие
Telegram preflight, readiness endpoint и monitor report добавляют его сами.
Не вписывайте время вручную и не переиспользуйте старый JSON: report без
`generated_at` либо старше окна freshness fail closed.

## Изолированный запуск

Ниже приложение запускается read-only, без сети. Trusted static `age` с recovery
host монтируется в exact path. Каталог evidence должен заранее существовать,
быть `0700` и принадлежать UID/GID оператора.

```bash
set -eu
umask 077
RECOVERY_UID="$(id -u)"
RECOVERY_GID="$(id -g)"
EVIDENCE_DIR=/secure-evidence/bibitasks-recovery-2026q3
install -d -m 0700 "$EVIDENCE_DIR"

docker run --rm --network none --read-only \
  --tmpfs /tmp:size=16m,mode=1777 \
  --user "$RECOVERY_UID:$RECOVERY_GID" \
  --cap-drop ALL --security-opt no-new-privileges:true \
  -v /usr/local/bin/age:/usr/local/bin/age:ro \
  -v /media/custodian/identity.agekey:/input/identity.agekey:ro \
  -v /media/offhost-a/bibitasks-production.age:/input/copy-a.age:ro \
  -v /media/offhost-b/bibitasks-production.age:/input/copy-b.age:ro \
  -v /secure-restore/restore-rehearsal/bibitasks.db:/input/bibitasks.db:ro \
  -v /secure-restore/restore-rehearsal/recovery-key-canaries.json:/input/recovery-key-canaries.json:ro \
  -v /secure-backup/snapshot/manifest.json:/input/manifest.json:ro \
  -v /secure-restore/restore-rehearsal/restore-report.json:/input/restore-report.json:ro \
  -v /secure-live/telegram-preflight.json:/input/preflight.json:ro \
  -v /secure-live/readiness.json:/input/readiness.json:ro \
  -v /secure-live/monitor-drill.json:/input/monitor.json:ro \
  -v "$EVIDENCE_DIR:/evidence" \
  ghcr.io/voglogpro/bibitasks@sha256:<64-hex-digest> \
  python scripts/secret_recovery_evidence.py \
    --encrypted-recovery-bundle /input/copy-a.age \
    --encrypted-recovery-bundle /input/copy-b.age \
    --age-identity-file /input/identity.agekey \
    --database /input/bibitasks.db \
    --recovery-key-canaries /input/recovery-key-canaries.json \
    --backup-manifest /input/manifest.json \
    --restore-report /input/restore-report.json \
    --commit <full-40-char-commit> \
    --image ghcr.io/voglogpro/bibitasks@sha256:<64-hex-digest> \
    --schema-version 295 \
    --release-version 'v2.10.0' \
    --preflight-report /input/preflight.json \
    --readiness-report /input/readiness.json \
    --monitor-canary-report /input/monitor.json \
    --output /evidence/secret-recovery.json
```

`age` получает identity и bundle только как `/proc/self/fd/<n>` и наследует
только эти descriptors. Environment subprocess очищен, stdout ограничен 64 KiB,
stdin закрыт, stderr отбрасывается, timeout — 30 секунд. Обе копии decryptятся;
их plaintext обязан совпасть.

После `ok=true` сохраните report в append-only CI artifact или внешнее
versioned/WORM-хранилище и передайте его в release gate. Не добавляйте в Git
bundle, identity, SQLite, canary из production, manifests/reports с operational
данными или output evidence. `.gitignore` — только страховка. Попавший в Git
секрет считается скомпрометированным и требует ротации.
