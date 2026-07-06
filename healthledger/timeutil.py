"""Timestamp and date parsing helpers."""
from __future__ import annotations

from healthledger.config import *  # noqa: F401,F403


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_ts(value: str | None, *, end_of_day: bool = False) -> str:
    """Normalise a timestamp to ISO8601 UTC. Accepts None/'now', full ISO strings,
    or 'YYYY-MM-DD'. Naive inputs are assumed UTC."""
    if value is None or str(value).strip().lower() in ("", "now"):
        return _now_iso()
    raw = str(value).strip()
    try:
        if DATE_ONLY_RE.fullmatch(raw):
            dt = datetime.strptime(raw, "%Y-%m-%d")
            if end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59)
        else:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"unrecognised timestamp {value!r}; use ISO8601 (2026-07-06T08:30) "
            f"or 'YYYY-MM-DD' or 'now'"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _range_bounds(since: str | None, until: str | None) -> tuple[str, str]:
    lo = _parse_ts(since) if since else "0000-01-01T00:00:00+00:00"
    hi = _parse_ts(until, end_of_day=True) if until else "9999-12-31T23:59:59+00:00"
    return lo, hi


def _parse_date(value: str | None, field: str, *, default_today: bool = False) -> str | None:
    if value is None or str(value).strip() == "":
        return datetime.now(timezone.utc).date().isoformat() if default_today else None
    return _parse_ts(str(value)).split("T", 1)[0]


def _date_or_now(value: str | None, field: str) -> str:
    parsed = _parse_date(value, field, default_today=True)
    if parsed is None:
        raise ValueError(f"{field} is required")
    return parsed
