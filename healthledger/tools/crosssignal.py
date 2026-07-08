"""Cross-signal reasoning: correlate, event impact, align, normalize."""
from healthledger.runtime import *  # noqa: F401,F403


@mcp.tool(annotations={"title": "Correlate two signals", "readOnlyHint": True, "idempotentHint": True, "statementType": "descriptive"})
def correlate_metrics(
    source_a: str,
    name_a: str,
    source_b: str,
    name_b: str,
    since: str | None = None,
    until: str | None = None,
    resample: str = "day",
    agg: str = "mean",
    method: str = "both",
    lag_days: int = 0,
    user: str | None = None,
) -> dict:
    """Correlate two health signals aligned onto a common time grid.

    Resamples each signal to one value per `resample` bucket (day/week/month)
    with `agg`, inner-joins the buckets they share, then computes Pearson and/or
    Spearman correlation with a two-sided p-value and the paired sample size.
    Either signal may come from any source: metric, wearable, lab, biomarker,
    substance.

    Args:
        source_a / name_a: first signal, e.g. source='metric' name='weight_kg'.
        source_b / name_b: second signal, e.g. source='lab' name='a1c_percent'.
        since / until: ISO8601 or 'YYYY-MM-DD' bounds (inclusive).
        resample: 'day' | 'week' | 'month' bucket granularity.
        agg: how to collapse multiple readings in a bucket
             ('mean','median','sum','min','max','first','last','count').
        method: 'pearson' | 'spearman' | 'both'.
        lag_days: only with resample='day'. Positive values pair each A bucket
            with the B bucket `lag_days` days earlier (tests whether B leads A).
        user: which person; defaults to the primary user.

    Correlation is descriptive association, never causation or diagnosis.
    """
    u = _tool_user(user, "correlate_metrics")
    if resample not in _RESAMPLE_BUCKETS:
        raise ValueError(f"resample must be one of {'|'.join(_RESAMPLE_BUCKETS)}")
    if method not in ("pearson", "spearman", "both"):
        raise ValueError("method must be pearson|spearman|both")
    if agg not in _AGG_FUNCS:
        raise ValueError(f"agg must be one of {'|'.join(_AGG_FUNCS)}")
    lag = int(lag_days)
    if lag and resample != "day":
        raise ValueError("lag_days is only supported with resample='day'")
    lo, hi = _range_bounds(since, until)
    with _db() as conn:
        skey_a, clean_a, _, items_a = _resolve_series(conn, u, source_a, name_a, lo, hi)
        skey_b, clean_b, _, items_b = _resolve_series(conn, u, source_b, name_b, lo, hi)
    _audit("correlate_metrics",
           f"{_audit_user(u)} a_hash={_fingerprint(clean_a)} b_hash={_fingerprint(clean_b)} "
           f"resample={resample} agg={agg} lag={lag}")
    grid_a = _resample_series(items_a, resample, agg)
    grid_b = _resample_series(items_b, resample, agg)

    def _shift(key: str) -> str:
        if not lag:
            return key
        return (datetime.fromisoformat(key).date() - timedelta(days=lag)).isoformat()

    paired = [(k, grid_a[k], grid_b[_shift(k)]) for k in sorted(grid_a) if _shift(k) in grid_b]
    xs = [p[1] for p in paired]
    ys = [p[2] for p in paired]
    combined_ids = _source_refs(items_a + items_b)
    latest_ts = max([it["ts"] for it in items_a + items_b], default=None)
    result = {
        "user": u,
        "series_a": {"source": skey_a, "name": clean_a,
                     "unit": items_a[-1]["unit"] if items_a else None,
                     "n_readings": len(items_a), "n_buckets": len(grid_a),
                     "source_ids": _source_refs(items_a)},
        "series_b": {"source": skey_b, "name": clean_b,
                     "unit": items_b[-1]["unit"] if items_b else None,
                     "n_readings": len(items_b), "n_buckets": len(grid_b),
                     "source_ids": _source_refs(items_b)},
        "resample": resample, "agg": agg, "lag_days": lag,
        "paired_n": len(paired),
        "overlap": ({"first_bucket": paired[0][0], "last_bucket": paired[-1][0]}
                    if paired else None),
        "disclaimer": "Descriptive association only; correlation is not causation or diagnosis.",
    }
    caveats: list[str] = []
    if len(paired) < 3:
        result["message"] = "need at least 3 shared buckets to correlate"
        caveats.append("Too few overlapping points for a meaningful correlation.")
        result["caveats"] = caveats
        return _envelope(
            value=[{"bucket": k, "a": a, "b": b} for k, a, b in paired],
            days_stale=_days_since(latest_ts) if latest_ts else None,
            source_ids=combined_ids,
            **result,
        )
    if len(paired) < 10:
        caveats.append(f"Only {len(paired)} paired points; the estimate is unstable and "
                       "p-values are rough.")
    caveats.append("Time-series points are autocorrelated, so true significance is weaker "
                   "than the p-value suggests.")
    if method in ("pearson", "both"):
        r = _pearson_r(xs, ys)
        if r is None:
            result["pearson"] = None
            caveats.append("Pearson undefined: a series had zero variance over the shared buckets.")
        else:
            result["pearson"] = {
                "r": round(r, 4), "p_value": _corr_p_value(r, len(paired)),
                "df": len(paired) - 2, "strength": _corr_strength(r),
                "direction": "positive" if r > 0 else ("negative" if r < 0 else "none"),
            }
    if method in ("spearman", "both"):
        rho = _pearson_r(_rankdata(xs), _rankdata(ys))
        if rho is None:
            result["spearman"] = None
        else:
            result["spearman"] = {
                "rho": round(rho, 4), "p_value": _corr_p_value(rho, len(paired)),
                "strength": _corr_strength(rho),
                "direction": "positive" if rho > 0 else ("negative" if rho < 0 else "none"),
            }
    result["caveats"] = caveats
    return _envelope(
        value=[{"bucket": k, "a": a, "b": b} for k, a, b in paired],
        days_stale=_days_since(latest_ts) if latest_ts else None,
        source_ids=combined_ids,
        **result,
    )


@mcp.tool(annotations={"title": "Analyze event impact", "readOnlyHint": True, "idempotentHint": True, "statementType": "descriptive"})
def analyze_event_impact(
    name: str,
    event_date: str,
    source: str = "metric",
    event_label: str | None = None,
    window_days: int | None = None,
    washout_days: int = 0,
    user: str | None = None,
) -> dict:
    """Estimate a signal's before/after change around a discrete event.

    Splits one signal at an anchor date (e.g. a medication start, procedure, or
    regimen change) into 'before' and 'after' groups, reports descriptive stats
    for each, and adds the difference in means plus a Welch t-test.

    Args:
        name: the signal name, e.g. 'resting_heart_rate' or 'a1c_percent'.
        event_date: the anchor date (ISO8601 or 'YYYY-MM-DD').
        source: 'metric' | 'wearable' | 'lab' | 'biomarker' | 'substance'.
        event_label: optional description of the event, echoed in the response.
        window_days: if set, only include readings within this many days on each
            side of the event; omit to use all available history.
        washout_days: exclude readings within this many days of the event on both
            sides (a washout gap) to skip transition-period noise.
        user: which person; defaults to the primary user.

    A before/after difference is descriptive, never proof the event caused it.
    """
    u = _tool_user(user, "analyze_event_impact")
    anchor = _parse_ts(event_date)
    anchor_dt = datetime.fromisoformat(anchor)
    wd = int(window_days) if window_days is not None else None
    wash = max(0, int(washout_days))
    if wd is not None:
        lo = (anchor_dt - timedelta(days=wd)).isoformat()
        hi = (anchor_dt + timedelta(days=wd)).replace(hour=23, minute=59, second=59).isoformat()
    else:
        lo, hi = _range_bounds(None, None)
    with _db() as conn:
        skey, clean, cfg, items = _resolve_series(conn, u, source, name, lo, hi)
    _audit("analyze_event_impact",
           f"{_audit_user(u)} source={skey} name_hash={_fingerprint(clean)} washout={wash}")
    before, after = [], []
    for it in items:
        it_dt = datetime.fromisoformat(it["ts"])
        if wash and abs((it_dt - anchor_dt).total_seconds()) / 86400.0 < wash:
            continue
        (before if it_dt < anchor_dt else after).append(it)
    latest = max(items, key=lambda it: it["ts"]) if items else None
    source_ids = {"before": _source_refs(before), "after": _source_refs(after)}
    out = {
        "user": u,
        "source": skey,
        "name": clean,
        "event": {"date": anchor, "label": event_label},
        "window_days": wd,
        "washout_days": wash,
        "before": _group_stats(before),
        "after": _group_stats(after),
        "disclaimer": "Descriptive before/after comparison; not proof of causation or medical advice.",
    }
    if before and after:
        mb, ma = out["before"]["mean"], out["after"]["mean"]
        change = round(ma - mb, 4)
        out["change"] = {
            "mean_before": mb,
            "mean_after": ma,
            "absolute_change": change,
            "percent_change": round((change / mb) * 100.0, 2) if mb else None,
            "direction": "increase" if change > 0 else ("decrease" if change < 0 else "no change"),
        }
        welch = _welch_t([it["value"] for it in before], [it["value"] for it in after])
        if welch:
            out["change"]["welch_t_test"] = welch
    else:
        out["message"] = "need readings on both sides of the event to compare"
    return _envelope(
        value=(out.get("change") or {}).get("absolute_change"),
        unit=latest["unit"] if latest else None,
        ref_low=latest.get("ref_low") if latest else None,
        ref_high=latest.get("ref_high") if latest else None,
        days_stale=_days_since(latest["ts"]) if latest else None,
        source_ids=source_ids,
        **out,
    )


@mcp.tool(annotations={"title": "Align multiple signals", "readOnlyHint": True, "idempotentHint": True})
def align_series(
    series_json: str,
    since: str | None = None,
    until: str | None = None,
    resample: str = "day",
    agg: str = "mean",
    join: str = "outer",
    limit: int = 500,
    user: str | None = None,
) -> dict:
    """Resample 2+ signals onto one shared time grid for side-by-side comparison.

    Takes a JSON array of signal specs and returns a single aligned table — one
    row per time bucket, one column per signal — so signals can be compared
    without hand-matching timestamps.

    Args:
        series_json: JSON array of specs, each {"source","name"} with optional
            "label" and per-series "agg". Example:
            '[{"source":"metric","name":"weight_kg"},
              {"source":"lab","name":"a1c_percent","agg":"last"}]'
        since / until: ISO8601 or 'YYYY-MM-DD' bounds (inclusive).
        resample: 'day' | 'week' | 'month' bucket granularity.
        agg: default bucket aggregation ('mean','median','sum','min','max',
             'first','last','count'); a spec's own "agg" overrides it.
        join: 'outer' (every bucket any signal has, missing entries null) or
              'inner' (only buckets every signal shares).
        limit: max rows returned (the most recent are kept if exceeded).
        user: which person; defaults to the primary user.

    Aligned descriptive values only; not diagnosis or medical advice.
    """
    u = _tool_user(user, "align_series")
    if resample not in _RESAMPLE_BUCKETS:
        raise ValueError(f"resample must be one of {'|'.join(_RESAMPLE_BUCKETS)}")
    if join not in ("outer", "inner"):
        raise ValueError("join must be outer|inner")
    if agg not in _AGG_FUNCS:
        raise ValueError(f"agg must be one of {'|'.join(_AGG_FUNCS)}")
    specs = _json_list(series_json, "series_json")
    if not 1 <= len(specs) <= 8:
        raise ValueError("series_json must contain between 1 and 8 signal specs")
    lim = _limit(limit, default=500)
    lo, hi = _range_bounds(since, until)
    grids: list[tuple[str, dict]] = []
    meta: list[dict] = []
    used_labels: set[str] = set()
    with _db() as conn:
        for spec in specs:
            if not isinstance(spec, dict):
                raise ValueError("each series spec must be a JSON object")
            skey, clean, cfg, items = _resolve_series(
                conn, u, spec.get("source"), spec.get("name"), lo, hi)
            spec_agg = spec.get("agg", agg)
            if spec_agg not in _AGG_FUNCS:
                raise ValueError(f"agg must be one of {'|'.join(_AGG_FUNCS)}")
            label = _optional_text(spec.get("label"), "label", max_chars=60) or f"{skey}:{clean}"
            base, n = label, 2
            while label in used_labels:
                label = f"{base}#{n}"
                n += 1
            used_labels.add(label)
            grid = _resample_series(items, resample, spec_agg)
            grids.append((label, grid))
            meta.append({"label": label, "source": skey, "name": clean,
                         "source_ids": _source_refs(items),
                         "unit": items[-1]["unit"] if items else None, "agg": spec_agg,
                         "n_readings": len(items), "n_buckets": len(grid)})
    _audit("align_series", f"{_audit_user(u)} series={len(specs)} resample={resample} join={join}")
    if join == "inner":
        keys: set | None = None
        for _, g in grids:
            keys = set(g) if keys is None else (keys & set(g))
        ordered = sorted(keys or set())
    else:
        allk: set = set()
        for _, g in grids:
            allk |= set(g)
        ordered = sorted(allk)
    total = len(ordered)
    truncated = total > lim
    if truncated:
        ordered = ordered[-lim:]
    grid_rows = [{"bucket": k, **{label: g.get(k) for label, g in grids}} for k in ordered]
    return {
        "user": u,
        "resample": resample,
        "default_agg": agg,
        "join": join,
        "series": meta,
        "bucket_count": total,
        "returned": len(grid_rows),
        "truncated": truncated,
        "grid": grid_rows,
        "disclaimer": "Aligned descriptive values only; not diagnosis or medical advice.",
    }


@mcp.tool(annotations={"title": "Normalize units & ranges", "readOnlyHint": True, "idempotentHint": True})
def normalize_series(
    name: str,
    source: str = "lab",
    to_unit: str | None = None,
    analyte: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
    user: str | None = None,
) -> dict:
    """Reconcile mixed units and reference ranges within one signal.

    Pulls a signal's readings, converts every value (and its reference range, for
    labs/biomarkers) to a single common unit, and adds a unitless 'reference
    position' so readings taken with different units or reference ranges become
    directly comparable.

    Args:
        name: the signal name, e.g. 'glucose' or 'a1c_percent'.
        source: 'lab' | 'biomarker' | 'metric' | 'wearable' | 'substance'.
        to_unit: target unit for all values; omit to use the most common unit
            already present in the series.
        analyte: analyte hint (e.g. 'glucose', 'cholesterol', 'creatinine') that
            unlocks mass<->molar conversions like mg/dL<->mmol/L; defaults to the
            signal name.
        since / until: ISO8601 or 'YYYY-MM-DD' bounds (inclusive).
        limit: max readings to return.
        user: which person; defaults to the primary user.

    'reference position' is 0 at the lower reference bound and 1 at the upper.
    Descriptive normalization only; not interpretation or diagnosis.
    """
    u = _tool_user(user, "normalize_series")
    lim = _limit(limit, default=200)
    lo, hi = _range_bounds(since, until)
    with _db() as conn:
        skey, clean, cfg, items = _resolve_series(conn, u, source, name, lo, hi)
    _audit("normalize_series", f"{_audit_user(u)} source={skey} name_hash={_fingerprint(clean)}")
    if not items:
        return {"user": u, "source": skey, "name": clean, "count": 0}
    analyte_key = _keyish(analyte, "analyte") if analyte else clean
    units_seen: dict = {}
    for it in items:
        nu = _norm_unit(it["unit"])
        units_seen[nu] = units_seen.get(nu, 0) + 1
    if to_unit is not None:
        target = _norm_unit(to_unit)
    else:
        ranked = sorted(units_seen.items(), key=lambda kv: (kv[0] is None, -kv[1]))
        target = ranked[0][0] if ranked else None
    rows: list[dict] = []
    converted_values: list[float] = []
    unconverted: set[str] = set()
    for it in items[-lim:]:
        conv_val, ok, tag = _convert_value(it["value"], it["unit"], target, analyte_key)
        row = {
            "id": it.get("id"),
            "ts": it["ts"],
            "original_value": round(it["value"], 6),
            "original_unit": it["unit"],
            "value": round(conv_val, 6) if (ok and conv_val is not None) else None,
            "unit": target,
            "converted": bool(ok and conv_val is not None),
            "conversion": tag,
        }
        if cfg["has_ref"]:
            rl, rh = it.get("ref_low"), it.get("ref_high")
            rl_c = _convert_value(rl, it["unit"], target, analyte_key)[0] if rl is not None else None
            rh_c = _convert_value(rh, it["unit"], target, analyte_key)[0] if rh is not None else None
            row["ref_low"] = round(rl_c, 6) if rl_c is not None else None
            row["ref_high"] = round(rh_c, 6) if rh_c is not None else None
            base_val = row["value"] if row["value"] is not None else it["value"]
            base_lo = rl_c if rl_c is not None else rl
            base_hi = rh_c if rh_c is not None else rh
            if base_lo is not None and base_hi is not None and base_hi != base_lo:
                row["reference_position"] = round((base_val - base_lo) / (base_hi - base_lo), 4)
                row["in_range"] = bool(base_lo <= base_val <= base_hi)
        if row["converted"]:
            converted_values.append(row["value"])
        else:
            unconverted.add(it["unit"] or "unitless")
        rows.append(row)
    summary = {
        "user": u,
        "source": skey,
        "name": clean,
        "analyte_hint": analyte_key,
        "target_unit": target,
        "units_seen": {(k or "unitless"): v for k, v in units_seen.items()},
        "count": len(rows),
        "converted_count": len(converted_values),
        "unconverted_units": sorted(unconverted),
        "source_ids": _source_refs(rows, table=cfg["table"]),
        "rows": rows,
        "disclaimer": "Unit/reference normalization only; not interpretation or diagnosis.",
    }
    if converted_values:
        summary["normalized_stats"] = {
            "unit": target,
            "min": round(min(converted_values), 4),
            "max": round(max(converted_values), 4),
            "mean": round(statistics.fmean(converted_values), 4),
            "median": round(statistics.median(converted_values), 4),
        }
    return summary
