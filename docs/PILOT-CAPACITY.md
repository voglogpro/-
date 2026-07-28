# Ограничения нагрузки пилота

Версия v2.10 запускается одним процессом с SQLite. Ограничители ниже защищают
управляемый пилот от резкого наплыва, но не превращают SQLite в горизонтально
масштабируемую production-базу.

## API и фотографии

До чтения JSON приложение резервирует один из ограниченных process-local
слотов:

- `API_READ_INFLIGHT_MAX=32` — одновременные GET/HEAD API-запросы;
- `API_WRITE_INFLIGHT_MAX=16` — одновременные изменяющие API-запросы;
- `API_HEAVY_INFLIGHT_MAX=4` — общий подлимит для
  `/api/tasks/complete` и `/api/admin/task/create`.

При отсутствии слота сервер отвечает `503 server_busy` и `Retry-After: 3`.
Клиент обязан повторить тот же запрос с прежним `operation_id`; существующие
проверки идемпотентности остаются источником истины. Персональные лимиты
`API_READS_PER_MIN` и `API_WRITES_PER_MIN` продолжают работать и отвечают `429`.

После допуска фото проходят отдельную ограниченную полосу:

- `MEDIA_NORMALIZE_CONCURRENCY=1` — одна активная Pillow-нормализация;
- `MEDIA_NORMALIZE_MAX_WAITERS=3` — не более трёх ожидающих работ;
- `MEDIA_NORMALIZE_WAIT_TIMEOUT_SEC=5` — максимальное ожидание слота.

Переполнение или таймаут возвращает `503 media_processing_busy` с
`Retry-After`. Лимиты исходного файла 2,5 МБ, 20 мегапикселей, четырёх фото и
HTTP-запроса 16 МиБ не изменены.

## Telegram inbox и outbox

`/health/ready` теперь показывает глубину и возраст обеих очередей:

- `telegram_inbox_pending`, `telegram_inbox_oldest_at`,
  `telegram_inbox_backlogged`;
- `outbox_pending`, `outbox_oldest_at`, `outbox_backlogged`;
- soft/hard пороги и число webhook-отказов из-за перегрузки.

При глубине inbox от `TELEGRAM_INBOX_SOFT_LIMIT=100`, глубине outbox от
`TELEGRAM_OUTBOX_SOFT_LIMIT=100` или возрасте старейшего активного элемента
больше `TELEGRAM_QUEUE_OLDEST_SOFT_SEC=300` readiness становится нездоровым и
оператор получает сигнал мониторинга.

Новый уникальный update сначала проверяется на дубль. Если после этого в inbox
уже `TELEGRAM_INBOX_HARD_LIMIT=500` активных записей, webhook ничего не пишет и
отвечает `503` с `Retry-After: 2`. Telegram Bot API повторяет доставку webhook
при любом ответе вне диапазона 2xx, поэтому update остаётся у Telegram. Уже
сохранённый дубль даже при заполненной очереди получает `200`, что исключает
бесконечный повтор обработанного update.

Один обработчик ограничен `TELEGRAM_HANDLER_TIMEOUT_SEC=120` (допустимо
10–300 секунд). После deadline update не помечается выполненным: отменяемый
handler остаётся под своей lease до фактической остановки, затем запись уходит
в существующий retry/dead flow. Основной worker в это время продолжает брать
следующие доступные updates.

## Показатели readiness

Защищённый токеном `/health/ready` также отдаёт:

- `api_capacity.active_*` и накопительные `rejected_*`;
- `media_processing_capacity.active`, `waiters`, `rejected`;
- `webhook_overload_rejected`.

Счётчики отказов накопительные с момента запуска процесса. Они нужны для
настройки порогов по результатам нагрузочного теста, а не для автоматического
увеличения лимитов.

## Обязательный тест перед расширением пилота

На копии production-конфигурации проверить одновременно минимум 100 первых
открытий Mini App, 50 заявок в минуту, 10 фотоотчётов по четыре фотографии и
20 Telegram updates в секунду. Условия прохождения: нет `database is locked`,
необработанных 500 и OOM; webhook p95 до 500 мс; память приложения ниже 600 МБ;
после окончания импульса обе очереди уходят ниже soft-порогов не более чем за
пять минут.

Перед несколькими экземплярами приложения необходим отдельный runtime на
PostgreSQL, private object storage и разделение API/inbox/outbox workers.
Существующий migration harness сам по себе этого не обеспечивает.
