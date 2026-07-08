"""Unified time-series resolver + resampling across all data sources."""
from __future__ import annotations

import statistics
from datetime import datetime

from healthledger.validation import _keyish, _required_text
from healthledger.db import _rows


# source key -> how to pull a dated numeric series from its table.
# created_col / source_col carry provenance (import time, origin) where the table
# has them, so callers can report recency and where a value came from.
_SERIES_SOURCES = {
    "metric": {
        "table": "metrics", "name_col": "metric", "ts_col": "ts",
        "value_col": "value", "unit_col": "unit",
        "date_mode": "full", "coalesce_created": False, "has_ref": False,
        "created_col": None, "source_col": None,
    },
    "wearable": {
        "table": "wearable_samples", "name_col": "sample_type", "ts_col": "start_ts",
        "value_col": "value", "unit_col": "unit",
        "date_mode": "full", "coalesce_created": False, "has_ref": False,
        "created_col": "created_ts", "source_col": "source_name",
    },
    "lab": {
        "table": "lab_results", "name_col": "analyte", "ts_col": "result_date",
        "value_col": "numeric_value", "unit_col": "unit",
        "date_mode": "date", "coalesce_created": True, "has_ref": True,
        "created_col": "created_ts", "source_col": None,
    },
    "biomarker": {
        "table": "biomarkers", "name_col": "biomarker", "ts_col": "measured_date",
        "value_col": "numeric_value", "unit_col": "unit",
        "date_mode": "date", "coalesce_created": True, "has_ref": True,
        "created_col": "created_ts", "source_col": "source",
    },
    "substance": {
        "table": "substance_use_logs", "name_col": "substance", "ts_col": "timestamp",
        "value_col": "amount", "unit_col": "unit",
        "date_mode": "full", "coalesce_created": False, "has_ref": False,
        "created_col": "created_ts", "source_col": None,
    },
}

_RESAMPLE_BUCKETS = ("day", "week", "month")
_AGG_FUNCS = ("mean", "median", "sum", "min", "max", "first", "last", "count")


def _series_source(source: str) -> tuple[str, dict]:
    key = _required_text(source, "source", max_chars=40).strip().lower()
    if key not in _SERIES_SOURCES:
        allowed = "|".join(sorted(_SERIES_SOURCES))
        raise ValueError(f"source must be one of {allowed}")
    return key, _SERIES_SOURCES[key]


def _to_full_ts(ts: str) -> str:
    """Promote a date-only stamp to an ISO datetime; leave datetimes untouched."""
    if ts and "T" not in ts and len(ts) >= 10:
        return ts[:10] + "T00:00:00+00:00"
    return ts


def _resolve_series(conn, user: str, source: str | None, name: str | None,
                    lo: str, hi: str) -> tuple[str, str, dict, list[dict]]:
    """Pull one dated numeric series from any supported source, ascending by time.

    Returns (source_key, normalized_name, source_config, items) where each item is
    {id, source_table, ts, value, unit[, ref_low, ref_high][, created_ts][, source]}
    and non-numeric rows are dropped. The table/id pair plus created_ts + source
    give callers what they need to cite the exact row and report recency/origin."""
    key, cfg = _series_source(source)
    clean = _keyish(_required_text(name, "name"), "name")
    ts_expr = f"COALESCE({cfg['ts_col']}, created_ts)" if cfg["coalesce_created"] else cfg["ts_col"]
    cols = [f"id AS id", f"{ts_expr} AS ts", f"{cfg['value_col']} AS value", f"{cfg['unit_col']} AS unit"]
    if cfg["has_ref"]:
        cols += ["ref_low AS ref_low", "ref_high AS ref_high"]
    if cfg.get("created_col"):
        cols.append(f"{cfg['created_col']} AS created_ts")
    if cfg.get("source_col"):
        cols.append(f"{cfg['source_col']} AS source")
    sql = (
        f"SELECT {', '.join(cols)} FROM {cfg['table']} "
        f"WHERE user=? AND {cfg['name_col']}=? AND {cfg['value_col']} IS NOT NULL"
    )
    args: list = [user, clean]
    if cfg["date_mode"] == "date":
        sql += f" AND substr({ts_expr}, 1, 10) BETWEEN ? AND ?"
        args += [lo.split("T", 1)[0], hi.split("T", 1)[0]]
    else:
        sql += f" AND {cfg['ts_col']} BETWEEN ? AND ?"
        args += [lo, hi]
    sql += f" ORDER BY {ts_expr} ASC"
    items: list[dict] = []
    for r in _rows(conn.execute(sql, args)):
        ts = r.get("ts")
        if ts is None:
            continue
        item = {
            "id": r.get("id"),
            "source_table": cfg["table"],
            "ts": _to_full_ts(ts),
            "value": float(r["value"]),
            "unit": r.get("unit"),
        }
        if cfg["has_ref"]:
            item["ref_low"] = r.get("ref_low")
            item["ref_high"] = r.get("ref_high")
        if cfg.get("created_col"):
            item["created_ts"] = r.get("created_ts")
        if cfg.get("source_col"):
            item["source"] = r.get("source")
        items.append(item)
    return key, clean, cfg, items


def _source_refs(items: list[dict], *, table: str | None = None) -> list[dict]:
    """Return get_record-ready source citations for resolved rows."""
    refs: list[dict] = []
    for item in items:
        row_id = item.get("id")
        source_table = table or item.get("source_table")
        if row_id is not None and source_table:
            refs.append({"table": source_table, "id": row_id})
    return refs


def _bucket_key(ts: str, bucket: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if bucket == "day":
        return dt.date().isoformat()
    if bucket == "week":
        return dt.strftime("%G-W%V")
    if bucket == "month":
        return dt.strftime("%Y-%m")
    raise ValueError(f"resample must be one of {'|'.join(_RESAMPLE_BUCKETS)}")


def _agg_values(values: list[float], agg: str) -> float:
    if agg == "mean":
        return statistics.fmean(values)
    if agg == "median":
        return statistics.median(values)
    if agg == "sum":
        return float(sum(values))
    if agg == "min":
        return min(values)
    if agg == "max":
        return max(values)
    if agg == "first":
        return values[0]
    if agg == "last":
        return values[-1]
    if agg == "count":
        return float(len(values))
    raise ValueError(f"agg must be one of {'|'.join(_AGG_FUNCS)}")


def _resample_series(items: list[dict], bucket: str, agg: str) -> dict[str, float]:
    """Collapse resolved items to one value per time bucket (items are time-ordered)."""
    grouped: dict[str, list[float]] = {}
    for it in items:
        grouped.setdefault(_bucket_key(it["ts"], bucket), []).append(it["value"])
    return {k: round(_agg_values(v, agg), 6) for k, v in grouped.items()}
