"""On-host watchdog for the controlled BibiTasks pilot.

The watchdog deliberately has no Docker socket and no database access.  It
observes the authenticated application readiness endpoint and the backup
scheduler's read-only status file, then delivers deduplicated Telegram alerts.
Secrets are read from Compose secret files and are never logged.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


READINESS_URL = "http://bibitasks:3000/health/ready"
MAX_DOCUMENT_BYTES = 64 * 1024
TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{30,100}$")
CHAT_RE = re.compile(r"^-100[0-9]{6,16}$")
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _positive_int(env, name: str, *, minimum: int, maximum: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw or not raw.isascii() or not raw.isdigit():
        raise ValueError(f"{name} must be an integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _absolute_file(raw: str, name: str, *, must_exist: bool) -> Path:
    path = Path(str(raw or ""))
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    if path.is_symlink():
        raise ValueError(f"{name} must not be a symlink")
    if must_exist:
        try:
            info = path.stat()
        except OSError as exc:
            raise ValueError(f"{name} is missing or unreadable") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError(f"{name} must be a small regular file")
        if stat.S_IMODE(info.st_mode) & 0o222:
            raise ValueError(f"{name} must be mounted read-only")
    return path


def _read_secret(path: Path, name: str) -> str:
    try:
        value = path.read_text("utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{name} is unreadable") from exc
    if not value or any(character.isspace() for character in value):
        raise ValueError(f"{name} must contain one non-empty value")
    return value


@dataclass(frozen=True)
class Config:
    alert_token: str
    alert_chat_id: str
    health_token: str
    backup_status_file: Path
    state_file: Path
    interval_seconds: int
    timeout_seconds: int
    failure_threshold: int
    delivery_failure_threshold: int
    reminder_seconds: int
    backup_rpo_seconds: int
    instance_label: str


def load_config(env=None) -> Config:
    values = os.environ if env is None else env
    if env is None and os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ValueError("long-lived pilot monitor must not run as root")
    if str(values.get("BIBITASKS_ENVIRONMENT", "")) != "production":
        raise ValueError("BIBITASKS_ENVIRONMENT must be production")
    if str(values.get("MONITOR_READINESS_URL", "")) != READINESS_URL:
        raise ValueError("MONITOR_READINESS_URL must use the fixed internal endpoint")
    alert_path = _absolute_file(
        values.get("MONITOR_ALERT_TOKEN_FILE", ""),
        "MONITOR_ALERT_TOKEN_FILE", must_exist=True,
    )
    health_path = _absolute_file(
        values.get("MONITOR_HEALTH_TOKEN_FILE", ""),
        "MONITOR_HEALTH_TOKEN_FILE", must_exist=True,
    )
    backup_path = _absolute_file(
        values.get("MONITOR_BACKUP_STATUS_FILE", ""),
        "MONITOR_BACKUP_STATUS_FILE", must_exist=False,
    )
    state_path = _absolute_file(
        values.get("MONITOR_STATE_FILE", ""),
        "MONITOR_STATE_FILE", must_exist=False,
    )
    alert_token = _read_secret(alert_path, "alert token")
    health_token = _read_secret(health_path, "health token")
    if not TOKEN_RE.fullmatch(alert_token):
        raise ValueError("alert token has an invalid Telegram token shape")
    if len(health_token) < 32 or len(health_token) > 256:
        raise ValueError("health token length is invalid")
    chat_id = str(values.get("MONITOR_ALERT_CHAT_ID", "")).strip()
    if not CHAT_RE.fullmatch(chat_id):
        raise ValueError("MONITOR_ALERT_CHAT_ID must be a Telegram supergroup ID")
    label = str(values.get("MONITOR_INSTANCE_LABEL", "")).strip()
    if not LABEL_RE.fullmatch(label):
        raise ValueError("MONITOR_INSTANCE_LABEL has an invalid shape")
    interval = _positive_int(
        values, "MONITOR_INTERVAL_SECONDS", minimum=30, maximum=300,
    )
    timeout = _positive_int(
        values, "MONITOR_TIMEOUT_SECONDS", minimum=2, maximum=15,
    )
    if timeout >= interval:
        raise ValueError("MONITOR_TIMEOUT_SECONDS must be below the interval")
    return Config(
        alert_token=alert_token,
        alert_chat_id=chat_id,
        health_token=health_token,
        backup_status_file=backup_path,
        state_file=state_path,
        interval_seconds=interval,
        timeout_seconds=timeout,
        failure_threshold=_positive_int(
            values, "MONITOR_FAILURE_THRESHOLD", minimum=2, maximum=10,
        ),
        delivery_failure_threshold=_positive_int(
            values, "MONITOR_DELIVERY_FAILURE_THRESHOLD", minimum=2, maximum=10,
        ),
        reminder_seconds=_positive_int(
            values, "MONITOR_REMINDER_SECONDS", minimum=900, maximum=86400,
        ),
        backup_rpo_seconds=_positive_int(
            values, "BACKUP_RPO_SECONDS", minimum=300, maximum=86400,
        ),
        instance_label=label,
    )


@dataclass(frozen=True)
class Observation:
    key: str
    title: str
    healthy: bool
    detail: str


def _decode_document(raw: bytes, name: str):
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"{name} is too large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object")
    return value


def _response_body(response) -> bytes:
    raw = response.read(MAX_DOCUMENT_BYTES + 1)
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise ValueError("readiness payload is too large")
    return raw


def read_readiness(config: Config, opener: Callable = urlopen):
    request = Request(
        READINESS_URL,
        headers={"Accept": "application/json", "X-Health-Token": config.health_token},
        method="GET",
    )
    try:
        response = opener(request, timeout=config.timeout_seconds)
        status = int(response.getcode())
        raw = _response_body(response)
    except HTTPError as exc:
        status = int(exc.code)
        raw = _response_body(exc)
    except (OSError, TimeoutError, URLError):
        return None, None
    try:
        return status, _decode_document(raw, "readiness payload")
    except ValueError:
        return status, None


def app_observations(config: Config, opener: Callable = urlopen):
    status, payload = read_readiness(config, opener)
    if payload is None:
        app = Observation(
            "application", "приложение", False,
            "readiness недоступен или вернул некорректный JSON",
        )
        queues = Observation(
            "dead_queues", "очереди Telegram", False,
            "состояние dead-очередей невозможно подтвердить",
        )
        return app, queues
    ready = status == 200 and payload.get("ok") is True
    app = Observation(
        "application", "приложение", ready,
        "readiness подтверждён" if ready else f"readiness HTTP {status}, ok не подтверждён",
    )
    outbox = payload.get("outbox_dead")
    inbox = payload.get("telegram_inbox_dead")
    counts_valid = (
        type(outbox) is int and type(inbox) is int and outbox >= 0 and inbox >= 0
    )
    queues_ok = counts_valid and outbox == 0 and inbox == 0
    detail = (
        f"outbox dead={outbox}, inbox dead={inbox}"
        if counts_valid else "readiness не содержит корректные счётчики dead-очередей"
    )
    return app, Observation("dead_queues", "очереди Telegram", queues_ok, detail)


def _aware_timestamp(value, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} is missing")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def backup_observation(config: Config, *, now: datetime | None = None):
    current = (now or utc_now()).astimezone(timezone.utc)
    path = config.backup_status_file
    try:
        if path.is_symlink():
            raise ValueError("symlink")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("unsafe status file")
        payload = _decode_document(path.read_bytes(), "backup status")
        success = _aware_timestamp(payload.get("last_success_at"), "last_success_at")
        attempted = _aware_timestamp(payload.get("attempted_at"), "attempted_at")
        age = (current - success).total_seconds()
        future = max(success, attempted) - current
        failures = payload.get("consecutive_failures")
        if type(failures) is not int or failures < 0:
            raise ValueError("invalid failure count")
        healthy = (
            -300 <= age <= config.backup_rpo_seconds
            and future.total_seconds() <= 300
            and failures == 0
            and payload.get("error") is None
        )
        minutes = max(0, int(age // 60))
        detail = f"последняя проверенная копия {minutes} мин назад; ошибок подряд: {failures}"
        return Observation("backup", "резервное копирование", healthy, detail)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return Observation(
            "backup", "резервное копирование", False,
            "корректный backup-status не найден",
        )


def collect_observations(config: Config, *, now=None, opener=urlopen):
    return [*app_observations(config, opener), backup_observation(config, now=now)]


def _blank_state():
    return {
        "version": 1, "last_check_at": None, "checks": {},
        "delivery": {
            "consecutive_failures": 0,
            "error_since": None,
            "last_error_at": None,
        },
    }


def _state_shape_valid(value) -> bool:
    if value.get("version") != 1 or not isinstance(value.get("checks"), dict):
        return False
    if value.get("last_check_at") is not None and not isinstance(value.get("last_check_at"), str):
        return False
    delivery = value.get("delivery")
    if not isinstance(delivery, dict):
        return False
    delivery_failures = delivery.get("consecutive_failures")
    if type(delivery_failures) is not int or not 0 <= delivery_failures <= 1_000_000:
        return False
    for name in ("error_since", "last_error_at"):
        raw = delivery.get(name)
        if raw is not None and (not isinstance(raw, str) or len(raw) > 64):
            return False
    allowed = {"application", "dead_queues", "backup", "monitor_state"}
    for key, item in value["checks"].items():
        if key not in allowed or not isinstance(item, dict):
            return False
        failures = item.get("failures", 0)
        if type(failures) is not int or not 0 <= failures <= 1_000_000:
            return False
        for name in ("alert_active", "last_healthy"):
            if item.get(name) is not None and type(item.get(name)) is not bool:
                return False
        for name in (
            "last_notified_at", "last_incident_delivered_at",
            "last_recovery_delivered_at",
        ):
            raw = item.get(name)
            if raw is not None and (not isinstance(raw, str) or len(raw) > 64):
                return False
        if not isinstance(item.get("detail", ""), str) or len(item.get("detail", "")) > 512:
            return False
        if type(item.get("pending_incident", False)) is not bool:
            return False
        if not isinstance(item.get("pending_incident_detail", ""), str):
            return False
        if len(item.get("pending_incident_detail", "")) > 512:
            return False
    return True


def load_state(path: Path):
    if not path.exists():
        return _blank_state(), False
    try:
        if path.is_symlink() or path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise ValueError("unsafe state")
        value = _decode_document(path.read_bytes(), "monitor state")
        if not _state_shape_valid(value):
            raise ValueError("unknown state version")
        return value, False
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return _blank_state(), True


def save_state(path: Path, state) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _seconds_since(raw, now: datetime) -> float | None:
    try:
        return (now - _aware_timestamp(raw, "timestamp")).total_seconds()
    except ValueError:
        return None


def plan_transitions(config: Config, state, observations, now: datetime):
    events = []
    checks = state.setdefault("checks", {})
    for observation in observations:
        previous = checks.get(observation.key)
        if not isinstance(previous, dict):
            previous = {}
        failures = 0 if observation.healthy else int(previous.get("failures") or 0) + 1
        active = previous.get("alert_active") is True
        last_notified = previous.get("last_notified_at")
        event = None
        event_observation = observation
        if previous.get("pending_incident") is True and not active:
            event = "incident"
            event_observation = Observation(
                observation.key, observation.title, False,
                str(previous.get("pending_incident_detail") or observation.detail),
            )
        elif observation.healthy and active:
            event = "recovery"
        elif not observation.healthy and not active and failures >= config.failure_threshold:
            event = "incident"
        elif not observation.healthy and active:
            elapsed = _seconds_since(last_notified, now)
            if elapsed is None or elapsed >= config.reminder_seconds:
                event = "reminder"
        checks[observation.key] = {
            "failures": failures,
            "alert_active": active,
            "last_notified_at": last_notified,
            "last_healthy": observation.healthy,
            "detail": observation.detail,
            "last_incident_delivered_at": previous.get("last_incident_delivered_at"),
            "last_recovery_delivered_at": previous.get("last_recovery_delivered_at"),
            "pending_incident": previous.get("pending_incident") is True,
            "pending_incident_detail": str(previous.get("pending_incident_detail") or ""),
        }
        if event:
            events.append((event, event_observation))
    state["last_check_at"] = now.isoformat()
    return events


def render_alert(config: Config, events, now: datetime) -> str:
    lines = [f"БибиЗадачи · {config.instance_label}"]
    labels = {"incident": "СБОЙ", "reminder": "НАПОМИНАНИЕ", "recovery": "ВОССТАНОВЛЕНО"}
    for event, observation in events:
        lines.append(f"{labels[event]} · {observation.title}: {observation.detail}")
    lines.append("UTC: " + now.strftime("%Y-%m-%d %H:%M:%S"))
    return "\n".join(lines)[:4000]


def send_alert(config: Config, text: str, opener: Callable = urlopen):
    body = json.dumps({
        "chat_id": config.alert_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{config.alert_token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        response = opener(request, timeout=config.timeout_seconds)
        status = int(response.getcode())
        payload = _decode_document(_response_body(response), "Telegram response")
    except HTTPError as exc:
        raise RuntimeError(f"Telegram alert returned HTTP {exc.code}") from None
    except (OSError, TimeoutError, URLError, ValueError):
        raise RuntimeError("Telegram alert transport failed") from None
    if status != 200 or payload.get("ok") is not True:
        raise RuntimeError("Telegram did not confirm alert delivery")
    result = payload.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    chat = result.get("chat") if isinstance(result, dict) else None
    returned_chat_id = chat.get("id") if isinstance(chat, dict) else None
    if type(message_id) is not int or message_id <= 0 or str(returned_chat_id) != config.alert_chat_id:
        raise RuntimeError("Telegram alert confirmation is incomplete")
    return message_id


def apply_delivered_events(state, events, now: datetime) -> None:
    for event, observation in events:
        item = state["checks"][observation.key]
        item["alert_active"] = event != "recovery"
        item["last_notified_at"] = now.isoformat()
        if event == "incident":
            item["last_incident_delivered_at"] = now.isoformat()
            item["pending_incident"] = False
            item["pending_incident_detail"] = ""
        elif event == "recovery":
            item["last_recovery_delivered_at"] = now.isoformat()


def monitor_report(config: Config, *, now=None):
    current = (now or utc_now()).astimezone(timezone.utc)
    state, recovered = load_state(config.state_file)
    checks = {}
    for key in ("application", "dead_queues", "backup", "monitor_state"):
        item = state.get("checks", {}).get(key)
        if not isinstance(item, dict):
            continue
        checks[key] = {
            "alert_active": item.get("alert_active") is True,
            "last_healthy": item.get("last_healthy") is True,
            "last_incident_delivered_at": item.get("last_incident_delivered_at"),
            "last_recovery_delivered_at": item.get("last_recovery_delivered_at"),
        }
    heartbeat_age = _seconds_since(state.get("last_check_at"), current)
    heartbeat_ok = (
        not recovered and heartbeat_age is not None
        and -300 <= heartbeat_age <= config.interval_seconds * 3
    )
    delivery = state.get("delivery", {})
    delivery_failures = int(delivery.get("consecutive_failures") or 0)
    delivery_ok = delivery_failures < config.delivery_failure_threshold
    required = ("application", "dead_queues", "backup")
    monitor_state_ok = (
        "monitor_state" not in checks
        or (
            checks["monitor_state"]["last_healthy"]
            and not checks["monitor_state"]["alert_active"]
        )
    )
    report_ok = heartbeat_ok and delivery_ok and monitor_state_ok and all(
        key in checks and checks[key]["last_healthy"]
        and not checks[key]["alert_active"]
        for key in required
    )
    return {
        "schema_version": 1,
        "ok": report_ok,
        "generated_at": current.isoformat(),
        "instance_label": config.instance_label,
        "heartbeat_at": state.get("last_check_at"),
        "heartbeat_ok": heartbeat_ok,
        "alert_delivery_ok": delivery_ok,
        "consecutive_delivery_failures": delivery_failures,
        "delivery_error_since": delivery.get("error_since"),
        "checks": checks,
    }


def run_cycle(config: Config, *, now=None, readiness_opener=urlopen, alert_opener=urlopen):
    current = (now or utc_now()).astimezone(timezone.utc)
    state, recovered = load_state(config.state_file)
    observations = collect_observations(config, now=current, opener=readiness_opener)
    if recovered:
        state.setdefault("checks", {})["monitor_state"] = {
            "failures": config.failure_threshold - 1,
            "alert_active": False,
            "last_notified_at": None,
            "last_healthy": False,
        }
        observations.append(Observation(
            "monitor_state", "состояние монитора", False,
            "локальное состояние было повреждено и безопасно пересоздано",
        ))
    elif (
        "monitor_state" in state.get("checks", {})
        and state["checks"]["monitor_state"].get("last_healthy") is False
        and state["checks"]["monitor_state"].get("alert_active") is not True
    ):
        observations.append(Observation(
            "monitor_state", "состояние монитора", False,
            "ожидается подтверждение alert о пересоздании state",
        ))
    elif "monitor_state" in state.get("checks", {}):
        observations.append(Observation(
            "monitor_state", "состояние монитора", True,
            "локальное состояние снова читается",
        ))
    events = plan_transitions(config, state, observations, current)
    delivery_error = None
    if events:
        try:
            send_alert(config, render_alert(config, events, current), opener=alert_opener)
            apply_delivered_events(state, events, current)
            state["delivery"] = {
                "consecutive_failures": 0,
                "error_since": None,
                "last_error_at": None,
            }
        except RuntimeError as exc:
            delivery_error = str(exc)
            delivery = state.setdefault("delivery", {})
            failures = int(delivery.get("consecutive_failures") or 0) + 1
            delivery.update(
                consecutive_failures=failures,
                error_since=delivery.get("error_since") or current.isoformat(),
                last_error_at=current.isoformat(),
            )
            for event, observation in events:
                if event == "incident":
                    item = state["checks"][observation.key]
                    item["pending_incident"] = True
                    item["pending_incident_detail"] = observation.detail
    save_state(config.state_file, state)
    return observations, events, delivery_error


def check_heartbeat(config: Config, *, now=None) -> None:
    state, recovered = load_state(config.state_file)
    if recovered or not config.state_file.exists():
        raise RuntimeError("monitor heartbeat is unavailable")
    current = (now or utc_now()).astimezone(timezone.utc)
    age = _seconds_since(state.get("last_check_at"), current)
    if age is None or age < -300 or age > config.interval_seconds * 3:
        raise RuntimeError("monitor heartbeat is stale")
    delivery = state.get("delivery", {})
    if int(delivery.get("consecutive_failures") or 0) >= config.delivery_failure_threshold:
        raise RuntimeError("monitor alert delivery is persistently failing")


def run(config: Config) -> None:
    while True:
        started = time.monotonic()
        _, _, error = run_cycle(config)
        if error:
            print(error, flush=True)
        delay = max(0.0, config.interval_seconds - (time.monotonic() - started))
        time.sleep(delay)


def main() -> int:
    parser = argparse.ArgumentParser(description="BibiTasks pilot watchdog")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--check-config", action="store_true")
    mode.add_argument("--healthcheck", action="store_true")
    mode.add_argument("--test-alert", action="store_true")
    mode.add_argument("--report", action="store_true")
    args = parser.parse_args()
    try:
        config = load_config()
        if args.check_config:
            return 0
        if args.healthcheck:
            check_heartbeat(config)
            return 0
        if args.test_alert:
            message_id = send_alert(
                config,
                f"БибиЗадачи · {config.instance_label}\nТЕСТ · канал мониторинга подключён",
            )
            print(json.dumps({
                "schema_version": 1,
                "ok": True,
                "event": "test_alert",
                "delivered_at": utc_now().isoformat(),
                "instance_label": config.instance_label,
                "message_id": message_id,
            }, sort_keys=True))
            return 0
        if args.report:
            print(json.dumps(monitor_report(config), sort_keys=True))
            return 0
        if args.once:
            _, _, error = run_cycle(config)
            return 1 if error else 0
        run(config)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"pilot monitor refused to run: {exc}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
