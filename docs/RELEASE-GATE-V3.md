# Двухфазный release gate v3

Gate разделяет неизменяемый кандидат и разрешение на production. Ни один
артефакт первой фазы не разрешает deployment.

## Фаза 1: release candidate

`scripts/release_candidate.py` проверяет GitHub/SLSA attestation образа и
создаёт новый JSON только через exclusive create с правами `0600`, вне Git
repository. В кандидате зафиксированы:

- полный commit, immutable GHCR digest, SQLite schema и semver приложения;
- точный SHA-256 backup manifest;
- SHA-256 и размер базы и pre-disaster recovery-key canary;
- четыре счётчика encrypted/active-NULL строк;
- `software_subject_sha256` и `promotion_subject_sha256` над canonical JSON.

Пример (каталог evidence заранее создаётся с mode `0700`):

```bash
python scripts/release_candidate.py \
  --commit "$COMMIT" --image "$IMAGE" --schema-version "$SCHEMA" \
  --application-version "$VERSION" --backup-manifest "$BACKUP/manifest.json" \
  --telegram-bot-id "$BOT_ID" --telegram-group-id "$GROUP_ID" \
  --miniapp-origin https://tasks.example.com \
  --health-origin https://health.example.com \
  --output /var/lib/bibitasks-evidence/release-candidate.json
```

`sqlite_volume_rollback.py plan` принимает этот JSON через
`--release-candidate`/`--release-candidate-sha256` (legacy aliases
`--release-record*` сохранены для v2.9.1). Plan, stage и verify report
переносят точные `candidate_sha256`, `software_subject_sha256` и
`promotion_subject_sha256`. Reader старого `record_version=2` сохранён только
для уже выпущенной v2.9.1; новые релизы обязаны использовать candidate v1.

## Фаза 2: неавторизующий validator v3

`scripts/final_release_record.py` сейчас является намеренно
**неавторизующим validator skeleton**. Он проверяет перечисленные ниже
артефакты, но в конце всегда возвращает ошибку и никогда не создаёт `go: true`:

1. candidate и его отдельно переданный SHA-256;
2. rollback verify report с теми же subject hashes и backup hashes;
3. secret recovery report v2 с exact commit/image/schema/version/manifest;
4. зелёные Telegram preflight, readiness и monitor reports;
5. подписанный внешний dead-man drill;
6. подписанный live E2E группы, бота, Mini App, фото задания и бонусов;
7. отдельное подписанное разрешение, которое содержит SHA-256 всех доказательств.

Dead-man, E2E и authorization подписываются тремя различными Ed25519-ключами:
разными должны быть и `key_id`, и фактические bytes public key.
Их public keys и issuer берутся только из явно переданного trust-root JSON:

```json
{
  "trust_roots_version": 1,
  "keys": [{
    "kind": "external_deadman",
    "key_id": "provider-key-2026",
    "issuer": "real-provider.example",
    "public_key_base64": "<32-byte Ed25519 public key, base64>",
    "enabled": true
  }]
}
```

SHA-256 trust-root JSON не передаётся тем же свободным CLI argument. Он должен
быть заранее закреплён оператором/контроллером в защищённой переменной
`BIBITASKS_RELEASE_TRUST_ROOT_SHA256`; несовпадение блокирует gate.

В trust roots должны быть отдельные entries для `external_deadman`, `live_e2e`
и `release_authorization`. Проект не содержит выдуманного issuer или public
key. Пока реальные trust roots и подписанные доказательства не получены, gate
обязан завершиться ошибкой и не создаёт record — это штатный `NO-GO`.

Каждый signed evidence — JSON с `evidence_version: 1`, точным `kind`, `issuer`,
UTC `generated_at`, обоими subject hashes, одним независимо сгенерированным
32-byte hex `challenge` и подписью:

```json
{"signature":{"algorithm":"ed25519","key_id":"...","value_base64":"..."}}
```

Подписывается canonical JSON всего документа без поля `signature`: UTF-8,
sorted keys, separators `,` и `:`, без ASCII escaping. Dead-man `result`
должен дословно подтверждать external observer, доставку incident и recovery.
Live E2E `result` должен подтверждать bot join, group, Mini App, task photo и
bonus flows. Authorization имеет `decision: "authorize"` и
`evidence_sha256` с точными hashes всех предыдущих evidence-файлов.

Freshness: preflight/readiness/monitor — максимум 5 минут; signed dead-man/E2E —
60 минут; rollback/recovery — 4 часа; authorization — 5 минут и не раньше всех
доказательств. Provenance повторно проверяется в final validator с жёстко
закреплёнными repository `voglogpro/-` и release workflow.

Случайные имена согласующих, CLI booleans и self-asserted quorum не являются
security gate. Final GO останется заблокирован, пока отдельно не реализованы и
не проверены: криптографический quorum двух хранителей recovery bundle,
защищённый single-use challenge ledger и deployment controller, проверяющий
подписанный final record непосредственно перед изменением production. До этого
никакой final record не создаётся — даже при полностью зелёных тестовых JSON.
