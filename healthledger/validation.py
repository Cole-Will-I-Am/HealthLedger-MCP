"""Input validation, normalization, and per-caller rate limiting."""
from __future__ import annotations

from healthledger.config import *  # noqa: F401,F403
from healthledger.config import _RATE_BUCKETS
from healthledger.audit import _audit, _fingerprint
from mcp.server.auth.middleware.auth_context import get_access_token


def _required_text(value: str | None, field: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    if value is None:
        raise ValueError(f"{field} is required")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} cannot be empty")
    if len(text) > max_chars:
        raise ValueError(f"{field} is too long; max {max_chars} characters")
    return text


def _optional_text(value: str | None, field: str, *, max_chars: int = MAX_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_chars:
        raise ValueError(f"{field} is too long; max {max_chars} characters")
    return text


def _keyish(value: str, field: str) -> str:
    text = re.sub(r"\s+", "_", _required_text(value, field, max_chars=80).lower())
    if not SAFE_KEY_RE.fullmatch(text):
        raise ValueError(
            f"{field} must start with a letter/number and contain only letters, "
            "numbers, underscore, dot, colon, slash, or hyphen"
        )
    return text


def _user(value: str | None) -> str:
    candidate = str(value).strip() if value is not None else ""
    return _required_text(candidate or DEFAULT_USER, "user", max_chars=80)


def _caller_key(user: str) -> str:
    try:
        token = get_access_token()
    except Exception:
        token = None
    if token is not None:
        claims = getattr(token, "claims", None) or {}
        principal = (
            claims.get("login")
            or getattr(token, "subject", None)
            or getattr(token, "client_id", None)
        )
        if principal:
            return f"auth:{str(principal).lower()}"
    return f"local:{_fingerprint(user)}"


def _rate_limit(tool: str, user: str) -> None:
    if not RATE_LIMIT_ENABLED:
        return
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    key = _caller_key(user)
    bucket = [stamp for stamp in _RATE_BUCKETS.get(key, []) if stamp >= cutoff]
    if len(bucket) >= RATE_LIMIT_CALLS:
        _RATE_BUCKETS[key] = bucket
        _audit("rate_limited", f"caller_hash={_fingerprint(key)} tool={tool}")
        raise RuntimeError(
            f"rate limit exceeded: {RATE_LIMIT_CALLS} calls per "
            f"{RATE_LIMIT_WINDOW_SECONDS} seconds"
        )
    bucket.append(now)
    _RATE_BUCKETS[key] = bucket


def _tool_user(value: str | None, tool: str) -> str:
    user = _user(value)
    _rate_limit(tool, user)
    return user


def _metric(value: str) -> str:
    return _keyish(value, "metric")


def _profile_key(value: str) -> str:
    return _keyish(value, "key")


def _status(value: str | None, *, default: str = "active", max_chars: int = 40) -> str:
    return _optional_text(value, "status", max_chars=max_chars) or default


def _category(value: str) -> str:
    cat = _required_text(value, "category", max_chars=32).lower()
    if cat not in EVENT_CATEGORIES:
        allowed = "|".join(sorted(EVENT_CATEGORIES))
        raise ValueError(f"category must be one of {allowed}")
    return cat


def _finite_float(value: float, field: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _optional_finite_float(value: float | None, field: str) -> float | None:
    if value is None:
        return None
    return _finite_float(value, field)


def _limit(value: int, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, MAX_ROWS))


def _export_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = MAX_EXPORT_ROWS
    return max(1, min(parsed, MAX_EXPORT_ROWS))


def _offset(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, parsed)


def _try_numeric(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return _finite_float(str(value).replace(",", "").strip(), "numeric_value")
    except (TypeError, ValueError):
        return None


def _json_text(value: str | None, field: str = "extra_json") -> str | None:
    text = _optional_text(value, field)
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{field} must be valid JSON") from e
    return json.dumps(parsed, separators=(",", ":"), sort_keys=True)


def _json_list(value: str, field: str, *, max_chars: int = MAX_BULK_JSON_CHARS) -> list:
    text = _required_text(value, field, max_chars=max_chars)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{field} must be valid JSON") from e
    if not isinstance(parsed, list):
        raise ValueError(f"{field} must be a JSON array")
    return parsed


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
