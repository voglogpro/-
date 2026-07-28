# Identity split и удаление профиля

Статус: проект миграции, **не реализовано**.

В текущей версии self-service удаление или обезличивание профиля отсутствует.
Есть только операторский контакт и регламент. Прямой `DELETE FROM members`
**запрещён**: Telegram `user_id` сейчас одновременно является ключом профиля,
получателем уведомлений и ссылкой в assignment, ledger, withdrawal, dispute и
audit-таблицах. После удаления строки `/api/state` также может создать профиль
заново при следующем входе.

До завершения всех gates ниже функция удаления профиля и соответствующее
утверждение в интерфейсе остаются **NO-GO**.

## Целевая модель

Бизнес-история должна ссылаться на внутренний неизменяемый `member_id`, а не на
Telegram ID:

```text
members(member_id, profile fields, lifecycle_state)
  └─ telegram_identities(member_id, lookup_hmac, encrypted_external_id,
                          key_version, state, revoked_at)

assignment / ledger / withdrawal / dispute / audit
  └─ member_id
```

- `member_id` не выводится пользователю и не используется как Telegram chat ID.
- `lookup_hmac` вычисляется отдельным versioned key и остаётся персональными
  псевдонимизированными данными, пока существует identity mapping.
- Полный Telegram ID хранится зашифрованным только пока нужен вход или
  адресная доставка.
- Финансовые суммы, статусы и обратные проводки не переписываются при удалении;
  удаляется связь с Telegram identity и лишние профильные поля.
- Нельзя подменять Telegram ID отрицательным числом в старых колонках: одна
  пропущенная ссылка оставит идентификатор, а delivery-код может принять
  псевдоним за реального Telegram-получателя.

## Этап 1. Инвентаризация и identity split

1. Создать `telegram_identities` и внутренний `member_id`.
2. Построить однозначную таблицу соответствия legacy Telegram ID → member ID.
3. Перевести на `member_id` все subject и actor references: profile/referral,
   tasks/assignments/evidence, ledger/awards, withdrawals/events, disputes,
   grants/reversals, role changes, operation registry, outbox, publications и
   community activity.
4. Добавить явный `subject_member_id` в outbox и inbox metadata; Telegram
   destination хранить отдельно от бизнес-субъекта.
5. Убрать raw ID из `event_key`, JSON/text payload и новых request fingerprints.
   Новые fingerprints должны быть keyed и versioned.
6. Включать `PRAGMA foreign_keys=ON` для каждого SQLite-соединения, выполнять
   `foreign_key_check` после миграции. Для PostgreSQL создать настоящие FK и
   выполнять cutover только после сверки ledger и всех ссылок.
7. Миграция должна быть повторяемой, fail-closed и откатываться целиком при
   неизвестном actor/subject или нарушенной финансовой сверке.

## Этап 2. Lifecycle и holds

Создать append-only сущности:

- `profile_erasure_requests` — UUID, `member_id`, source, state, version,
  operation/request identity и timestamps;
- `profile_erasure_events` — структурированные события без свободного PII;
- `profile_erasure_holds` — reason code, source reference, review/release data;
- `erasure_message_cleanup` — попытки удаления уже отправленных сообщений.

Базовый автомат:

```text
verification_pending
  -> resolution_required
  -> cooling_off
  -> held | processing
  -> completed

verification_pending / resolution_required / cooling_off -> cancelled
```

После подтверждения участник не может брать новые задания, получать новые
начисления/награды или создавать referral links. Ему остаются только действия,
необходимые для закрытия существующей работы: просмотр статуса, release/submit,
допустимый dispute и урегулирование перевода.

Finalizer обязан fail-closed создавать hold при:

- assignment в `claimed` или `review`;
- dispute в `pending` или `manual_required`;
- withdrawal в `pending` или `processing`;
- grant reversal в `pending` или `manual_required`;
- незавершённой смене административной роли;
- ненулевом балансе;
- активном admin authority, особенно origin `env`;
- in-flight outbox/media operation;
- повреждённых или отсутствующих terminal timestamps;
- утверждённом legal hold с reason code и датой пересмотра.

Нельзя молча обнулять баланс, автоматически отвергать отчёт или закрывать спор
ради удаления профиля. Такие состояния завершаются существующими аудируемыми
доменными операциями.

Lifecycle guard должен выполняться внутри той же write transaction, что claim,
grant, award, referral reward, withdrawal, admin decision и другие изменения
субъекта. Одного HTTP middleware недостаточно: target может изменять администратор
или background worker.

## Этап 3. Подтверждение и API

Self-service flow:

1. `POST /api/profile/erasure/request` принимает свежий Telegram initData и
   UUID операции, создаёт только `verification_pending`.
2. Бот отправляет тому же Telegram ID кнопку с одноразовым opaque callback.
3. В БД хранится только hash токена; токен имеет короткий срок, привязан к
   request и `CallbackQuery.from.id`, потребляется атомарно один раз.
4. После bot callback запускается resolution/cooling-off. Повтор callback или
   request возвращает тот же результат.
5. `POST /api/profile/erasure/cancel` использует UUID и CAS по `version` и
   доступен только до необратимой фазы.
6. `GET /api/profile/erasure` показывает plain-language holds, удаляемые и
   сохраняемые категории и доступные действия.

Operator request по username, письму или скриншоту не подтверждает личность.
Оператор может создать case, но окончательное подтверждение отправляется
найденному Telegram-аккаунту. Процедура для утраченного аккаунта требует
отдельной проверки личности и maker-checker; публичного `force delete` быть не
должно. Администратор сначала штатно снимает полномочия, а env authority
удаляется из production-конфигурации до финализации.

## Этап 4. Минимизация и purge

После разрешения holds finalizer в повторяемых фазах:

1. отзывает referral tokens и прекращает новые subject-bound deliveries;
2. удаляет product events и analytics mapping;
3. очищает имя, username, legacy phone, город, анкету, теги, pending city и
   свободные профильные комментарии;
4. удаляет или ставит на существующий evidence retention фотографии, адреса и
   свободные task/dispute notes;
5. очищает subject-bound outbox payload/event key и ставит известные Telegram
   message IDs в cleanup queue;
6. удаляет identity ciphertext/HMAC после окончания оснований хранения;
7. сохраняет только внутренний member ID, ledger, суммы, статусы, structured
   reason codes и append-only evidence выполнения запроса;
8. переводит request в `completed` одной CAS-операцией.

Уже доставленное Telegram-сообщение может не удалиться из-за ограничений
платформы или пользовательской копии. Результат попытки фиксируется честно;
приложение не заявляет гарантированное удаление из Telegram.

## Этап 5. Backup и restore manifest

Удаление только из active DB недостаточно: restore старой копии может вернуть
identity. Нужен отдельно защищённый replayable manifest с keyed lookup HMAC,
версией ключа, completed timestamp и retention не короче максимального срока
backup плюс restore window.

После любого restore до открытия трафика:

1. загрузить manifest и применить все erasure generations;
2. повторить profile/identity/payload/media cleanup;
3. сверить watermark и финансовые инварианты;
4. запускать readiness только после успешного replay.

Provider lifecycle должен удалять просроченные backup generations. После expiry
последней способной воскресить профиль копии lookup HMAC также удаляется по
утверждённому сроку.

## Blocking test gates

- миграция сохраняет ledger totals, balances, operation IDs и число записей;
- ни одна legacy subject/actor ссылка не остаётся raw Telegram ID;
- все FK валидны; неизвестная ссылка откатывает миграцию;
- stale/unsigned initData, чужой/истёкший/replayed callback отклоняются;
- одна незавершённая заявка на участника; operation replay идемпотентен, а
  изменённое тело с тем же UUID даёт conflict;
- request гоняется параллельно с claim/grant/award/referral/withdraw и ровно одна
  сторона побеждает без частичного состояния;
- cancel против finalizer защищён CAS; finalizer переживает падение после каждой
  фазы и безопасно продолжается;
- каждый hold имеет позитивный и негативный тест, malformed timestamps держат
  данные fail-closed;
- после completion поиск не находит прежний Telegram ID, имя, username, город,
  about/tags в обычных колонках, JSON, event keys и request hashes;
- retained ledger остаётся финансово идентичным и связан только с member ID;
- старый initData не воскрешает completed профиль;
- backup до удаления + manifest replay не восстанавливает identity, а readiness
  закрыт до успешного replay;
- Telegram cleanup success и platform-expired failure оба наблюдаемы и не
  превращаются в ложное утверждение об удалении.

## Юридическая граница

Этот документ задаёт техническую модель, но не определяет правовое основание и
срок хранения. До реализации оператор, юрист и финансовый владелец утверждают:

- какие ledger/withdrawal записи являются обязательной отчётностью и на какой
  срок сохраняются;
- когда ненулевой остаток можно урегулировать и допустим ли явный отказ от него;
- правила legal hold, пересмотра и доступа;
- форму подтверждения уничтожения;
- срок erasure manifest и backup lifecycle;
- корректные формулировки «удаление», «псевдонимизация» и «обезличивание».

До этого retained внутренний ID, HMAC, account fingerprint и иные linkable
данные считаются псевдонимизированными персональными данными, а не анонимными.
