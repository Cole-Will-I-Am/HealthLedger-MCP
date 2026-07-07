"""Tool-boundary guardrails for generated HealthLedger prose."""
from __future__ import annotations

import re


_FORBIDDEN_PATTERNS = [
    r"\byou (have|are diagnosed with)\b",
    r"\bthis (means|indicates) you (have|should take)\b",
    r"\bstart taking\b",
    r"\bstop taking\b",
    r"\bincrease your dose\b",
    r"\bi recommend\b",
]


def _assert_descriptive(text: str, field: str) -> str:
    """Reject generated prose that reads as clinical instruction."""
    lowered = text.lower()
    for pattern in _FORBIDDEN_PATTERNS:
        if re.search(pattern, lowered):
            raise ValueError(
                f"{field} reads as clinical instruction, not a "
                f"description of stored data: pattern {pattern!r} matched"
            )
    return text
