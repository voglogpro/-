# Управляемое вступление в сообщество

Этот регламент связывает публичную группу, `@BbGalterbot` и Mini App без обхода регистрации. Доступ к заданиям по-прежнему определяется одобрением анкеты; вступление в группу — отдельный управляемый процесс. Реферал подтверждается только после двух фактов: анкета одобрена и Telegram прислал достоверное подтверждение членства.

## Инварианты

- Бот должен быть администратором публичной supergroup с правом `can_invite_users`.
- В публичной группе должен быть включён `join_by_request`, иначе пользователь сможет обойти управляемую ссылку через публичный username.
- Рабочая ссылка создаётся именно ботом с `creates_join_request=true`.
- В базе хранится SHA-256 ссылки, а не сама ссылка. Не помещайте ссылку или токен бота в Git, issue, CI-лог или чат.
- Webhook/polling явно запрашивает `chat_join_request` и `chat_member`.
- `chat_member` — авторитетное подтверждение вступления; один join не может дважды подтвердить referral или начислить награду.

## Создание ссылки

Запускайте на доверенной машине, где production env доступен как файл с правами `0600`.

```bash
# Dry-run: проверяет токен, username и план, ничего не меняет.
python scripts/telegram_join_request_link.py \
  --env-file /etc/bibitasks/bibitasks.env

# Apply разрешён только после точного подтверждения username.
python scripts/telegram_join_request_link.py \
  --env-file /etc/bibitasks/bibitasks.env \
  --apply --confirm-bot @BbGalterbot
```

При первом запуске ответ содержит `invite_url` и `persist_invite_url=true`. Сразу внесите URL в защищённый env как `JOIN_REQUEST_INVITE_URL`, не копируя его в репозиторий. Повторный apply редактирует уже известную ссылку, а не создаёт новую.

Затем установите:

```text
JOIN_REQUEST_ADMISSION_ENABLED=true
JOIN_REQUEST_APPLICATION_SLA_HOURS=72
```

Перезапустите приложение и выполните:

```bash
python scripts/telegram_preflight.py \
  --env-file /etc/bibitasks/bibitasks.env
```

Preflight должен подтвердить `join_by_request`, `can_invite_users` и наличие `chat_join_request` в `allowed_updates`. Принадлежность самой ссылки доказывается результатом apply и реальным acceptance-сценарием; публичная страница `t.me` этого не доказывает.

## Поведение системы

- Новая заявка через проверенную ссылку получает `awaiting_application` или `awaiting_review`.
- После одобрения анкеты durable outbox отправляет `approveChatJoinRequest` вне транзакции базы.
- После отклонения анкеты outbox отправляет `declineChatJoinRequest`.
- Заявка без анкеты старше `JOIN_REQUEST_APPLICATION_SLA_HOURS` автоматически ставится на decline.
- Чужая, отозванная или не принадлежащая боту ссылка получает `manual_required`; автоматическое approve запрещено.
- Ошибка доставки после исчерпания retry получает `manual_required`. В admin overview видны состояние, последняя ошибка и история ручного повтора.
- Ручной повтор выполняется через `POST /api/admin/join-request/retry` с `request_key`, решением, причиной и новым UUID `operation_id`. Повтор того же UUID идемпотентен.

## Live acceptance

До пилота проверьте на отдельном тестовом пользователе:

1. Вход через управляемую ссылку создаёт join request, но не пускает в группу автоматически.
2. До анкеты скаут видит `awaiting_application`.
3. После анкеты скаут видит `awaiting_review`.
4. Одобрение анкеты приводит к одобрению заявки в Telegram.
5. `chat_member` переводит состояние в `joined`.
6. Повтор события не создаёт второй referral, outbox-event или бонус.
7. Отклонение анкеты отклоняет pending join request.
8. Вход через публичный `@bbbikefan` также требует заявку.
9. Сценарий пройден в Telegram Android, iOS и Desktop.

Без этих доказательств Telegram release gate остаётся `NO-GO`.
