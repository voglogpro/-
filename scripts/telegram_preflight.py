"""Read-only Telegram acceptance checks for a BibiTasks pilot deployment.

The script never sends or edits messages. It only calls Telegram get* methods
and prints a redacted JSON report. Exit code is zero only when every required
check passes; warnings are allowed.
"""

from __future__ import annotations

import json
import os
import sys
import argparse
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - production lock includes python-dotenv
    load_dotenv = None


ApiCall = Callable[[str, dict[str, object] | None], dict[str, object]]
BOT_COMMANDS = [
    {"command": "start", "description": "Начать работу"},
    {"command": "tasks", "description": "Открыть задания"},
    {"command": "profile", "description": "Профиль и бибибонусы"},
    {"command": "balance", "description": "Баланс и минуты поездки"},
    {"command": "help", "description": "Инструкция и команды"},
]


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def telegram_call(token: str, method: str, params=None, timeout=15):
    """Call a read-only Bot API method without ever including token in errors."""
    endpoint = f"https://api.telegram.org/bot{token}/{method}"
    payload = urllib.parse.urlencode(params or {}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Telegram API transport error: {type(exc).__name__}") from None
    if not data.get("ok"):
        code = data.get("error_code", "unknown")
        description = str(data.get("description", "request rejected"))[:160]
        raise RuntimeError(f"Telegram API {code}: {description}")
    result = data.get("result")
    return result if isinstance(result, dict) else {"value": result}


def _integer(env, name):
    raw = str(env.get(name, "")).strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _username(value):
    return str(value or "").strip().lstrip("@").casefold()


def _origin(value):
    parsed = urllib.parse.urlparse(str(value or "").strip())
    try:
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.netloc or port not in (None, 443):
        return ""
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return ""
    if parsed.path not in ("", "/"):
        return ""
    return f"https://{parsed.netloc}".rstrip("/")


def _truthy(value):
    return str(value or "").strip().casefold() in {"1", "true", "yes"}


def _canonical_app_url(value):
    parsed = urllib.parse.urlparse(str(value or "").strip())
    try:
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme != "https" or not parsed.netloc or port not in (None, 443)
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        return ""
    return f"https://{parsed.netloc}/"


def run_preflight(env=None, api_call: ApiCall | None = None):
    env = os.environ if env is None else env
    checks: list[Check] = []

    def add(name, ok, good, bad, *, warning=False):
        checks.append(Check(name, "pass" if ok else ("warn" if warning else "fail"), good if ok else bad))

    token = str(env.get("BOT_TOKEN", "")).strip()
    bot_username = _username(env.get("BOT_USERNAME"))
    group_username = _username(env.get("GROUP_USERNAME"))
    group_id = _integer(env, "GROUP_ID")
    ops_group_id = _integer(env, "OPS_GROUP_ID")
    update_mode = str(env.get("TELEGRAM_UPDATE_MODE", "")).strip().casefold()
    public_origin = _origin(env.get("PUBLIC_BASE_URL"))
    mini_app_url = _canonical_app_url(env.get("MINI_APP_URL"))
    webapp_shortname = str(env.get("WEBAPP_SHORTNAME", "")).strip()
    require_main_mini_app = _truthy(env.get("PREFLIGHT_REQUIRE_MAIN_MINI_APP"))
    route_id = str(env.get("WEBHOOK_ROUTE_ID", "")).strip()
    topic_names = (
        "TOPIC_NEWS", "TOPIC_CHAT", "TOPIC_WORK", "TOPIC_FRANCHISE",
        "OPS_TOPIC_TASKS",
    )
    topic_ids = {name: _integer(env, name) for name in topic_names}
    expected_bot_name = str(env.get("PREFLIGHT_EXPECTED_BOT_NAME", "Бибибайк")).strip()
    expected_group_title = str(env.get("PREFLIGHT_EXPECTED_GROUP_TITLE", "Бибибайк")).strip()
    expected_description = str(env.get("BOT_PROFILE_DESCRIPTION", "")).strip()
    expected_short_description = str(env.get("BOT_PROFILE_SHORT_DESCRIPTION", "")).strip()
    expected_menu_text = str(env.get("BOT_MENU_TEXT", "")).strip()

    add("BOT_TOKEN", bool(token and ":" in token), "configured", "missing or malformed")
    add("BOT_USERNAME", bool(bot_username), "configured", "missing")
    add("GROUP_ID", bool(group_id and group_id < 0), "numeric public chat ID configured", "must be a negative numeric chat ID")
    add("OPS_GROUP_ID", bool(ops_group_id and ops_group_id < 0), "numeric private OPS ID configured", "must be a negative numeric chat ID")
    add("chat separation", bool(group_id and ops_group_id and group_id != ops_group_id), "public and OPS chats differ", "public and OPS chats must differ")
    add("PUBLIC_BASE_URL", bool(public_origin), "HTTPS origin configured", "must be an HTTPS origin without path/query")
    add("MINI_APP_URL", bool(mini_app_url and mini_app_url == public_origin + "/"), "matches deployment root", "must be the HTTPS deployment root")
    add("WEBAPP_SHORTNAME", bool(webapp_shortname), "configured", "missing")
    add("bot profile copy", bool(expected_description and expected_short_description and expected_menu_text), "configured", "description, short description or menu text is missing")
    add(
        "topic IDs",
        all(value is not None and value > 0 for value in topic_ids.values())
        and len({topic_ids[name] for name in topic_names[:4]}) == 4,
        "all topic IDs are explicit and public topics differ",
        "all topic IDs must be positive and public topic IDs must differ",
    )
    add("update mode", update_mode in {"webhook", "polling"}, update_mode or "configured", "must be webhook or polling")
    add(
        "main Mini App policy",
        update_mode != "webhook" or require_main_mini_app,
        "required for webhook production",
        "PREFLIGHT_REQUIRE_MAIN_MINI_APP must be true in webhook production",
    )
    if any(item.status == "fail" for item in checks):
        return _report(checks)

    call = api_call or (lambda method, params=None: telegram_call(token, method, params))

    def invoke(method, params=None):
        try:
            return call(method, params)
        except Exception as exc:  # report is intentionally redacted
            checks.append(Check(method, "fail", str(exc).replace(token, "[redacted]")[:220]))
            return None

    me = invoke("getMe")
    if not me:
        return _report(checks)
    bot_id = int(me.get("id") or 0)
    actual_username = _username(me.get("username"))
    add("bot username", actual_username == bot_username, f"@{actual_username}", f"expected @{bot_username}, got @{actual_username or 'unknown'}")
    add("bot group access", bool(me.get("can_join_groups")), "bot can join groups", "BotFather forbids group access")
    if update_mode == "webhook" or require_main_mini_app:
        add(
            "main Mini App",
            bool(me.get("has_main_web_app")),
            "BotFather reports a Main Mini App",
            "BotFather does not report a Main Mini App",
        )

    bot_name = invoke("getMyName")
    actual_name = str((bot_name or {}).get("name", "")).strip()
    add(
        "bot brand name",
        actual_name == expected_bot_name,
        actual_name,
        f"expected exact name {expected_bot_name!r}, got {actual_name!r}",
    )
    description = invoke("getMyDescription")
    actual_description = str((description or {}).get("description", "")).strip()
    add(
        "bot description", actual_description == expected_description,
        "matches release copy", "missing or differs from release copy",
    )
    short_description = invoke("getMyShortDescription")
    actual_short_description = str(
        (short_description or {}).get("short_description", "")
    ).strip()
    add(
        "bot short description", actual_short_description == expected_short_description,
        "matches release copy", "missing or differs from release copy",
    )
    commands = invoke("getMyCommands")
    actual_commands = [
        {
            "command": str(item.get("command", "")),
            "description": str(item.get("description", "")),
        }
        for item in (commands or {}).get("value", []) if isinstance(item, dict)
    ]
    add(
        "bot commands", actual_commands == BOT_COMMANDS,
        "exact release commands configured", "commands or descriptions differ from release copy",
    )

    public_chat = invoke("getChat", {"chat_id": group_id})
    ops_chat = invoke("getChat", {"chat_id": ops_group_id})
    if public_chat:
        add("public chat type", public_chat.get("type") == "supergroup", "supergroup", f"unexpected type {public_chat.get('type')!r}")
        add("public forum mode", bool(public_chat.get("is_forum")), "topics enabled", "topics/forum mode is disabled")
        actual_group_username = _username(public_chat.get("username"))
        add("public group username", actual_group_username == group_username, f"@{actual_group_username}", f"expected @{group_username}, got @{actual_group_username or 'private'}")
        title = str(public_chat.get("title", "")).strip()
        add("public group brand", expected_group_title.casefold() in title.casefold(), title, f"expected title containing {expected_group_title!r}, got {title!r}")
    if ops_chat:
        add("OPS chat type", ops_chat.get("type") == "supergroup", "supergroup", f"unexpected type {ops_chat.get('type')!r}")
        add("OPS forum mode", bool(ops_chat.get("is_forum")), "topics enabled", "topics/forum mode is disabled")
        add("OPS privacy", not ops_chat.get("username"), "no public username", "OPS has a public username; addresses/photos may be discoverable")

    public_member = invoke("getChatMember", {"chat_id": group_id, "user_id": bot_id})
    if public_member:
        add("public bot admin", public_member.get("status") == "administrator", "administrator", f"status is {public_member.get('status')!r}")
        add("public delete permission", bool(public_member.get("can_delete_messages")), "can delete service/moderated messages", "can_delete_messages is missing")
    ops_member = invoke("getChatMember", {"chat_id": ops_group_id, "user_id": bot_id})
    if ops_member:
        active = ops_member.get("status") in {"member", "administrator", "creator"}
        add("OPS bot membership", active, str(ops_member.get("status")), f"status is {ops_member.get('status')!r}")

    if update_mode == "webhook":
        expected_webhook = f"{public_origin}/telegram/webhook/{route_id}"
        webhook = invoke("getWebhookInfo")
        if webhook:
            actual_webhook = str(webhook.get("url", "")).strip()
            add("webhook URL", bool(route_id and actual_webhook == expected_webhook), "matches configured HTTPS route", "missing or differs from configured route")
            pending = int(webhook.get("pending_update_count") or 0)
            add("webhook backlog", pending < 100, f"{pending} pending updates", f"backlog is {pending}", warning=True)
            add("webhook last error", not webhook.get("last_error_date"), "no recorded delivery error", "Telegram reports a recent webhook delivery error", warning=True)
    else:
        checks.append(Check("webhook mode", "warn", "polling is acceptable only for a controlled single-instance pilot"))

    menu = invoke("getChatMenuButton")
    if menu and menu.get("type") == "web_app":
        menu_url = _canonical_app_url((menu.get("web_app") or {}).get("url", ""))
        menu_text = str(menu.get("text", "")).strip()
        add("menu Mini App URL", menu_url == mini_app_url and menu_text == expected_menu_text, "menu button points to the exact Mini App URL", "menu button text or URL differs from release config")
    else:
        checks.append(Check(
            "menu Mini App",
            "fail" if update_mode == "webhook" else "warn",
            "default menu button is not a web_app",
        ))

    return _report(checks)


def _report(checks):
    counts = {status: sum(item.status == status for item in checks) for status in ("pass", "warn", "fail")}
    return {
        "ok": counts["fail"] == 0,
        "summary": counts,
        "checks": [asdict(item) for item in checks],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Read-only Telegram production preflight",
    )
    parser.add_argument(
        "--env-file", type=Path,
        help="explicit secrets file; process environment keeps precedence",
    )
    args = parser.parse_args()
    if load_dotenv is not None:
        if args.env_file:
            env_path = args.env_file.expanduser().resolve()
            if not env_path.is_file():
                parser.error("--env-file must point to an existing regular file")
            load_dotenv(dotenv_path=env_path, override=False)
        else:
            load_dotenv(override=False)
    report = run_preflight()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
