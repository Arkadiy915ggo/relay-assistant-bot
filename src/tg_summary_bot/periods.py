from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone


PERIOD_RE = re.compile(r"^(?P<num>\d+)(?P<unit>m|h|d|w)$", re.IGNORECASE)


def parse_period(value: str) -> timedelta:
    normalized = value.strip().lower()
    if normalized in {"today", "сегодня"}:
        now = datetime.now(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return now - start

    match = PERIOD_RE.match(normalized)
    if not match:
        raise ValueError("Use a period like 30m, 6h, 24h, 7d, 2w, today")

    amount = int(match.group("num"))
    unit = match.group("unit")
    if amount <= 0:
        raise ValueError("Period must be greater than zero")
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    raise ValueError("Unsupported period")


def format_period(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"today", "сегодня"}:
        return "сегодня"
    return normalized
