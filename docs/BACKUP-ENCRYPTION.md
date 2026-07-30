# Шифрование резервных копий

Пилот и production создают только authenticated-encrypted backup. В каталоге
назначения остаются два файла: `manifest.json` без секретов и
`payload.tar.aes256gcm`. SQLite, recovery canary и media никогда не публикуются
туда открытым текстом.

## Ключ

Ключ — отдельный root-owned файл `0600`, не `.env` и не значение переменной
окружения. Формат файла:

```json
{"format":"bibitasks-backup-key-v1","key_b64":"<BASE64URL-32-BYTES>","key_version":"pilot-2026-07"}
```

Создавайте файл непосредственно на защищённом VPS с `umask 077`. Для генерации
канонического содержимого используйте `scripts.backup_crypto.key_document` и
32 байта из `secrets.token_bytes(32)`; не печатайте результат в терминал и не
добавляйте файл в Git. Хранитель восстановления должен иметь отдельную
защищённую копию каждой версии ключа. Потеря версии ключа означает потерю всех
backup, созданных этой версией.

В non-secret deploy env задаются только путь и версия:

```dotenv
BACKUP_ENCRYPTION_KEY_FILE=/etc/bibitasks/backup-encryption.key
BACKUP_ENCRYPTION_KEY_VERSION=pilot-2026-07
```

`pilot_host_preflight.py` машинно проверяет абсолютный путь, regular/non-symlink,
root ownership, mode `0600`, формат, длину ключа и совпадение версии, но не
выводит путь или содержимое в отчёт.

## Plaintext scratch

SQLite online backup API требует seekable snapshot. Поэтому единственная
временная plaintext-копия создаётся в выделенном container `tmpfs`, затем
потоково упаковывается прямо в AES-256-GCM ciphertext без промежуточного tar.
Runtime повторно сверяет `/proc/self/mountinfo`: принимается только точный mount
root типа `tmpfs`/`ramfs`. На host должен быть отключён swap; preflight это
проверяет. При любом исключении private scratch и незавершённый ciphertext
удаляются.

`compose.pilot.yaml` задаёт:

- `/run/bibitasks-backup-plaintext` как отдельный private tmpfs;
- `/run/secrets/backup_encryption_key` как read-only secret;
- `network_mode: none`, read-only root filesystem и dropped capabilities.

Размер tmpfs (`512m`) должен превышать SQLite snapshot плюс локальные media;
лимит памяти backup container выше (`768m`), чтобы tmpfs не превышал cgroup cap.
Если места недостаточно, backup завершается ошибкой и healthcheck становится
красным; скрипт не переключается на disk tempfile.

## Что подписано

AES-256-GCM аутентифицирует ciphertext и AAD с `method`, `key_version` и SHA-256
внутреннего manifest. Внешний manifest содержит ciphertext path/size/SHA-256,
nonce и tag. Restore сначала проверяет SHA-256, затем GCM tag, digest внутреннего
manifest, точное совпадение внешней metadata и полный allowlist файлов.

Wrong key, изменённый ciphertext/manifest, symlink, hardlink, traversal path,
лишний или отсутствующий файл останавливают restore до публикации target.
Отчёт restore содержит только method, key version и digests — без ключа и
локальных путей.

## Restore rehearsal

Restore требует ту же версию key file и отдельный tmpfs/ramfs scratch. В
контейнере передайте `BACKUP_ENCRYPTION_KEY_FILE` и
`BACKUP_PLAINTEXT_TMP_DIR`; не передавайте key bytes через CLI/env. Target
создаётся только после полной аутентификации, SQLite `integrity_check`, проверки
schema/recovery counts и media checksums.

Plaintext-совместимость доступна только как явно указанный
`--allow-plaintext-dev`. CLI отклоняет этот флаг при
любом окружении кроме allowlist `dev|development|test|testing`; пустое значение
тоже отклоняется. Такой режим предназначен лишь для
старых локальных fixtures и не является migration path для production backup.

Пилот намеренно использует только local media: backup container работает с
`network_mode: none`. S3 backup/restore и S3 rollback в production запрещены
fail-closed, потому что текущий контракт не может доказать atomic no-overwrite и
end-to-end digest объекта у внешнего провайдера. `--allow-s3-dev` существует
только для изолированных тестов с тем же dev/test allowlist. Переход на S3
требует отдельного reviewed workflow, а не включения сети этому scheduler.

При ротации сначала установите новый key file/version, пройдите host preflight,
создайте backup, выполните restore rehearsal и только затем снимайте старую
версию с активного VPS. Старый ключ хранится до истечения retention всех
связанных ciphertext.

Rollback plan сохраняет `backup_key_version`. Если активный deploy env уже
указывает на другую версию, `apply` требует явный `--backup-key-file` с retained
root-owned key; содержимое и версия проверяются до создания restore container.
Legacy release record v2 и plaintext production backup не принимаются — для них
нужен новый release candidate, связанный с encrypted manifest.
