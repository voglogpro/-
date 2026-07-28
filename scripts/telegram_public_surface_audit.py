"""Audit the public Telegram group, bot and named Mini App without secrets.

This complements ``telegram_preflight.py``: it deliberately uses only public
``t.me`` preview pages, so an owner can detect stale branding or a broken named
Mini App before sharing a launch link.  It never calls the Bot API and never
needs ``BOT_TOKEN``.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - production lock includes python-dotenv
    load_dotenv = None


USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
SHORTNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,64}$")
MAX_PAGE_BYTES = 1_048_576
Fetch = Callable[[str], str]


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


class _PreviewParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.actions: list[dict[str, str]] = []
        self._action: dict[str, str] | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "meta":
            key = str(values.get("property") or values.get("name") or "")
            if key:
                self.meta[key.casefold()] = str(values.get("content") or "")
        elif tag == "a":
            classes = str(values.get("class") or "").split()
            if any(item.startswith("tgme_action_button") for item in classes):
                self._action = {"href": str(values.get("href") or ""), "text": ""}

    def handle_data(self, data):
        if self._action is not None:
            self._action["text"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self._action is not None:
            self._action["text"] = self._action["text"].strip()
            self.actions.append(self._action)
            self._action = None


def _username(value):
    return str(value or "").strip().lstrip("@")


def _fetch_page(url, *, timeout=15):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "BibiTasks-public-surface-audit/1.0",
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname != "t.me":
                raise RuntimeError("unexpected redirect outside t.me")
            content_type = str(response.headers.get("Content-Type") or "")
            if "text/html" not in content_type.casefold():
                raise RuntimeError("Telegram preview did not return HTML")
            payload = response.read(MAX_PAGE_BYTES + 1)
            if len(payload) > MAX_PAGE_BYTES:
                raise RuntimeError("Telegram preview exceeds size limit")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(
            f"Telegram public preview transport error: {type(exc).__name__}"
        ) from None
    return payload.decode("utf-8", errors="strict")


def _preview(html):
    parser = _PreviewParser()
    parser.feed(html)
    return parser


def _telegram_action(parser, *, username, appname=""):
    expected_user = username.casefold()
    expected_app = appname.casefold()
    for action in parser.actions:
        parsed = urllib.parse.urlparse(action["href"])
        if parsed.scheme != "tg" or parsed.netloc != "resolve":
            continue
        query = urllib.parse.parse_qs(parsed.query)
        domain = str((query.get("domain") or [""])[0]).casefold()
        actual_app = str((query.get("appname") or [""])[0]).casefold()
        if domain == expected_user and actual_app == expected_app:
            return action
    return None


def run_public_surface_audit(env=None, fetch: Fetch | None = None):
    env = os.environ if env is None else env
    checks: list[Check] = []

    def add(name, ok, good, bad, *, warning=False):
        status = "pass" if ok else ("warn" if warning else "fail")
        checks.append(Check(name, status, good if ok else bad))

    bot_username = _username(env.get("BOT_USERNAME", "BbGalterbot"))
    group_username = _username(env.get("GROUP_USERNAME", "bbbikefan"))
    shortname = str(env.get("WEBAPP_SHORTNAME", "bibibike") or "").strip()
    expected_bot_name = str(
        env.get("PREFLIGHT_EXPECTED_BOT_NAME", "БибиЗадачи · Бибибайк")
    ).strip()
    expected_group_title = str(
        env.get(
            "PREFLIGHT_EXPECTED_GROUP_TITLE",
            "Бибибайк | Сообщество помощников",
        )
    ).strip()
    expected_group_description = str(
        env.get("PREFLIGHT_EXPECTED_GROUP_DESCRIPTION", "")
    ).strip()

    add(
        "BOT_USERNAME", bool(USERNAME_RE.fullmatch(bot_username)),
        f"@{bot_username}", "missing or malformed",
    )
    add(
        "GROUP_USERNAME", bool(USERNAME_RE.fullmatch(group_username)),
        f"@{group_username}", "missing or malformed",
    )
    add(
        "WEBAPP_SHORTNAME", bool(SHORTNAME_RE.fullmatch(shortname)),
        shortname, "missing or malformed",
    )
    add(
        "expected public copy",
        bool(expected_bot_name and expected_group_title and expected_group_description),
        "configured", "bot name, group title or group description is missing",
    )
    if any(item.status == "fail" for item in checks):
        return _report(checks)

    get = fetch or _fetch_page
    pages = {
        "group": f"https://t.me/{group_username}",
        "bot": f"https://t.me/{bot_username}",
        "mini_app": f"https://t.me/{bot_username}/{shortname}",
    }
    parsed: dict[str, _PreviewParser] = {}
    # These public pages are independent. Fetch them together so one slow
    # Telegram CDN edge cannot multiply the operator's preflight duration.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        pending = {
            kind: executor.submit(get, url) for kind, url in pages.items()
        }
        for kind in pages:
            try:
                parsed[kind] = _preview(pending[kind].result())
                add(f"{kind} public preview", True, "reachable over HTTPS", "")
            except Exception as exc:
                add(
                    f"{kind} public preview", False, "",
                    f"unavailable: {type(exc).__name__}: {str(exc)[:140]}",
                )

    group = parsed.get("group")
    if group is not None:
        title = group.meta.get("og:title", "").strip()
        description = group.meta.get("og:description", "").strip()
        add(
            "public group brand", title == expected_group_title, title,
            f"expected exact title {expected_group_title!r}, got {title!r}",
        )
        add(
            "public group route copy",
            description == expected_group_description,
            "description matches approved bot route and bonus copy",
            "description differs from approved bot route and bonus copy",
        )

    bot = parsed.get("bot")
    if bot is not None:
        title = bot.meta.get("og:title", "").strip()
        add(
            "public bot brand", title == expected_bot_name, title,
            f"expected exact name {expected_bot_name!r}, got {title!r}",
        )
        add(
            "public bot launch action",
            _telegram_action(bot, username=bot_username) is not None,
            "Start Bot action resolves to the expected username",
            "missing or points to another Telegram target",
        )
        image = bot.meta.get("og:image", "").strip()
        add(
            "public bot avatar", bool(image), "published",
            "no public avatar is visible", warning=True,
        )

    mini_app = parsed.get("mini_app")
    if mini_app is not None:
        action = _telegram_action(
            mini_app, username=bot_username, appname=shortname,
        )
        # t.me serializes arbitrary /bot/appname paths into an Open App action,
        # including appnames that do not exist. This is useful link-shape
        # evidence, but deliberately never becomes a release PASS.
        checks.append(Check(
            "named Mini App registration",
            "warn",
            (
                f"@{bot_username}/{shortname} is serialized as an Open App link; "
                "this does not prove registration or target URL"
                if action is not None else
                "public preview gives no registration evidence; verify in "
                "BotFather and real Telegram clients"
            ),
        ))
        title = mini_app.meta.get("og:title", "").strip()
        add(
            "named Mini App brand", title == expected_bot_name, title,
            f"expected exact name {expected_bot_name!r}, got {title!r}",
        )

    return _report(checks)


def _report(checks):
    counts = {
        status: sum(item.status == status for item in checks)
        for status in ("pass", "warn", "fail")
    }
    return {
        "report_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "public_t_me_surface_no_secrets",
        "ok": counts["fail"] == 0,
        "summary": counts,
        "checks": [asdict(item) for item in checks],
        "limitations": [
            "does not prove Bot API permissions, webhook health or backend reachability",
            "t.me serializes unknown appnames, so it cannot prove Named Mini App registration",
            "avatar presence does not prove that the approved artwork is installed",
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description="Read-only public t.me group/bot/Mini App audit",
    )
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 60:
        parser.error("--timeout must be between 1 and 60 seconds")
    if load_dotenv is not None:
        if args.env_file:
            env_path = args.env_file.expanduser().resolve()
            if not env_path.is_file():
                parser.error("--env-file must point to an existing regular file")
            load_dotenv(dotenv_path=env_path, override=False)
        else:
            load_dotenv(override=False)
    report = run_public_surface_audit(
        fetch=lambda url: _fetch_page(url, timeout=args.timeout),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
