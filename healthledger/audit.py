"""Audit logging and value fingerprinting."""
from __future__ import annotations

from healthledger.config import *  # noqa: F401,F403


def _audit(event: str, detail: str) -> None:
    try:
        ts = datetime.now(timezone.utc).isoformat()
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(AUDIT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(AUDIT_LOG, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{ts}\t{event}\t{detail}\n")
    except Exception:
        pass  # auditing must never break a tool call


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _audit_user(user: str) -> str:
    return f"user_hash={_fingerprint(user)}"
