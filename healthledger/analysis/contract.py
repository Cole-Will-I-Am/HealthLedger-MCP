"""Shared output contracts for analysis tools."""
from __future__ import annotations


def _envelope(
    *,
    value,
    unit=None,
    ref_low=None,
    ref_high=None,
    ref_text=None,
    days_stale=None,
    source_ids=None,
    **extra,
) -> dict:
    """Every analysis tool's return value flows through this.

    Guarantees the four load-bearing keys are always present, even when null,
    so a caller can detect "missing" vs "omitted".
    """
    return {
        "value": value,
        "unit": unit,
        "reference_range": (
            {"low": ref_low, "high": ref_high, "text": ref_text}
            if (ref_low is not None or ref_high is not None or ref_text)
            else None
        ),
        "recency": (
            {"days_stale": days_stale}
            if days_stale is not None
            else {"days_stale": None, "note": "no dated value found"}
        ),
        "source_ids": source_ids or [],
        **extra,
    }
