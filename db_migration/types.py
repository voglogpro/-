"""Strict, PII-safe SQLite scalar converters for the PostgreSQL rehearsal."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone


class ConversionError(ValueError):
    pass


def parse_bool01(value, *, nullable=True):
    if value is None and nullable:
        return None
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    raise ConversionError("expected SQLite boolean 0 or 1")


def parse_bigint(value, *, nullable=True):
    if value is None and nullable:
        return None
    if type(value) is not int:
        raise ConversionError("expected SQLite integer without coercion")
    result = value
    if not -(2**63) <= result < 2**63:
        raise ConversionError("integer is outside signed BIGINT range")
    return result


def parse_utc(value, *, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConversionError("expected ISO timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConversionError("invalid ISO timestamp") from exc
    if result.tzinfo is None:
        raise ConversionError("timestamp must include timezone")
    return result.astimezone(timezone.utc)


def parse_date(value, *, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ConversionError("expected ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ConversionError("invalid ISO date") from exc


def parse_json(value, *, shape=None, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ConversionError("expected encoded JSON")
    try:
        result = json.loads(
            value,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ConversionError("non-finite JSON number is forbidden")
            ),
        )
    except json.JSONDecodeError as exc:
        raise ConversionError("invalid JSON") from exc
    if shape == "object" and not isinstance(result, dict):
        raise ConversionError("expected JSON object")
    if shape == "array" and not isinstance(result, list):
        raise ConversionError("expected JSON array")
    return result
