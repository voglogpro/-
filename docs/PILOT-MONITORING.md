# Мониторинг controlled pilot

Контейнер `monitor` проверяет раз в 30 секунд три независимых сигнала:

- закрытый `/health/ready` приложения: база, storage, Telegram receiver и workers;
- `outbox_dead` и `telegram_inbox_dead` из того же подписанного readiness-ответа;
- атомарный `backup-status.json`: последняя проверенная копия должна быть моложе
  `BACKUP_RPO_SECONDS`, а последняя попытка — без ошибки.

Monitor не имеет Docker socket, доступа к базе или пользовательским фотографиям.
Он видит только внутренний HTTP endpoint, read-only volume со статусом backup,
два Compose secret и собственный state-volume.

## Alert-канал и секреты

Создайте отдельного Telegram-бота для мониторинга и отдельную приватную
supergroup. Боту достаточно права отправлять сообщения — права администратора
не нужны. Основной `BOT_TOKEN` использовать нельзя: отдельные credentials
сохраняют канал диагностики при отзыве или компрометации рабочего token.

Bootstrap создаёт root-owned файлы `0600`:

- `/etc/bibitasks/monitor-alert-bot-token`;
- `/etc/bibitasks/monitor-health-token` — точная копия сгенерированного
  `HEALTH_TOKEN`, без вывода значения в терминал.

Compose передаёт их только сервису `monitor` как `/run/secrets/*`. Token не
попадает в environment, Compose config, state-файл или текст ошибки. Host
preflight проверяет тип, владельца и права файлов, но не читает содержимое.
Короткий launcher стартует с root только для чтения этих root-owned `0600`
mounts, копирует их в приватный tmpfs с правами `0400`, очищает supplementary
groups и необратимо переходит на UID/GID `10001`. Сам watchdog отказывается
работать с UID 0. Launcher получает только `CHOWN`, `SETGID`, `SETUID`, после
перехода fail-closed проверяет по `/proc/self/status`, что `CapPrm=CapEff=0`;
весь путь дополнительно проверяется настоящим Compose-run в CI.

## Дедупликация

Incident отправляется после двух последовательных неуспешных циклов. Пока
состояние не изменилось, повторов нет; через `MONITOR_REMINDER_SECONDS` приходит
одно напоминание. Recovery отправляется один раз после восстановления.

Состояние incident/recovery меняется только после ответа Telegram `ok=true`.
При сетевой ошибке то же сообщение будет повторено в следующем цикле. Атомарный
state-файл и heartbeat находятся в named volume `monitor_state`, поэтому рестарт
контейнера не создаёт повторный incident. Повреждение state не игнорируется:
monitor пересоздаёт его и создаёт отдельный диагностический alert.

Неуспешная доставка incident сохраняется как pending и повторяется, даже если
приложение успело восстановиться; после неё отдельно доставляется recovery.
После трёх последовательных ошибок Telegram (`MONITOR_DELIVERY_FAILURE_THRESHOLD`)
healthcheck и `--report` становятся fail: свежий heartbeat больше не маскирует
сломанный канал оповещения.

## Приёмка

После старта стека:

```bash
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml ps
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml \
  exec -T --user 10001:10001 monitor python scripts/pilot_monitor.py --healthcheck
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml \
  exec -T --user 10001:10001 monitor python scripts/pilot_monitor.py --test-alert
```

Ожидается `healthy` для `bibitasks`, `backup`, `monitor`, `caddy` и одно тестовое
сообщение в закрытой alert-группе. Значения secret в доказательства не копируйте.

Для безопасной проверки incident/recovery на пилотном стенде временно остановите
только приложение, не backup и не monitor:

```bash
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml stop bibitasks
# дождитесь incident (обычно до 60 секунд)
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml up -d bibitasks
# дождитесь recovery и снова проверьте весь stack
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml ps
```

После recovery сохраните redacted machine-readable доказательство:

```bash
docker compose --env-file /etc/bibitasks/deploy.env -f compose.pilot.yaml \
  exec -T --user 10001:10001 monitor python scripts/pilot_monitor.py --report \
  > "$EVIDENCE_DIR/monitor-drill.json"
```

В JSON нет token, chat ID или текста readiness. Для `application` должны быть
непустыми `last_incident_delivered_at` и `last_recovery_delivered_at`, оба позже
начала drill, `alert_active=false`, `last_healthy=true`, `heartbeat_ok=true`,
`alert_delivery_ok=true`.

Проводите эту проверку до допуска реальных участников и фиксируйте UTC-время
incident/recovery. Не выполняйте её во время пользовательской работы.

## Реакция

- `приложение`: проверить `docker compose ps` и redacted container logs;
- `очереди Telegram`: не начислять вручную повторно, сначала разобрать dead
  update/outbox по operation ID и штатному redrive;
- `резервное копирование`: остановить рискованные изменения, восстановить
  off-host mount, дождаться новой проверенной копии и затем recovery;
- нет heartbeat у `monitor`: проверить сам контейнер, secrets и сеть до Telegram.

Ротация alert token выполняется через новый root-owned файл и пересоздание только
`monitor`; старый token отзывается после успешного `--test-alert` новым.

## Обязательный независимый dead-man

On-host monitor не может отправить сообщение при выключении VPS, потере питания
или полной сетевой изоляции. Docker также не перезапускает контейнер только из-за
статуса `unhealthy`. Поэтому один этот monitor не закрывает P0 и не разрешает GO.

До controlled pilot обязательно настройте независимый внешний dead-man на другом
хосте или у внешнего provider. Он проверяет public HTTPS/TLS `/health/live` и
свежий heartbeat/health самого `monitor`, а оповещает через второй канал.
Host-level supervisor полезен дополнительно, но не заменяет внешний dead-man: он
не увидит потерю питания VPS или полную сетевую изоляцию. Остановите `monitor`,
затем имитируйте недоступность VPS и зафиксируйте incident/recovery внешнего
контроля. Без timestamp и evidence обоих событий — NO-GO.
