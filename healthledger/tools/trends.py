"""Advanced trend intelligence for a single signal."""
from healthledger.runtime import *  # noqa: F401,F403


@mcp.tool(annotations={"title": "Analyze trend (advanced)", "readOnlyHint": True, "idempotentHint": True, "statementType": "descriptive"})
def analyze_trend(
    name: str,
    source: str = "metric",
    since: str | None = None,
    until: str | None = None,
    baseline_window_days: int = 180,
    outlier_threshold: float = 3.5,
    user: str | None = None,
) -> dict:
    """Trend intelligence for one signal — beyond a single straight line.

    Pulls a signal's dated numeric readings and returns, in one call:
      * trend — least-squares slope with a standard error, 95% CI, p-value, and
        an honest 'distinguishable from flat / treat as noise' verdict;
      * baseline — the latest reading framed against your own recent median and
        typical range (Q1-Q3), the way a clinician reads a value;
      * rate_of_change — recent vs earlier slope, exposing acceleration;
      * outliers — points flagged by a robust median/MAD z-score, not silently
        averaged into the mean;
      * shape — whether a straight line is the right model at all (weak fit,
        residual runs, or a better-fitting quadratic) and whether linear
        extrapolation is advisable for bounded or cyclical signals;
      * change_point — the single most likely regime shift, if any.

    Args:
        name: the signal name, e.g. 'weight_kg', 'a1c_percent', 'resting_heart_rate'.
        source: 'metric' | 'wearable' | 'lab' | 'biomarker' | 'substance'.
        since / until: ISO8601 or 'YYYY-MM-DD' bounds (inclusive).
        baseline_window_days: lookback for the baseline median/range (default 180).
        outlier_threshold: modified z-score cutoff for outliers (default 3.5).
        user: which person; defaults to the primary user.

    Everything here is descriptive statistics — not diagnosis or medical advice.
    """
    u = _tool_user(user, "analyze_trend")
    lo, hi = _range_bounds(since, until)
    with _db() as conn:
        skey, clean, cfg, items = _resolve_series(conn, u, source, name, lo, hi)
    _audit("analyze_trend", f"{_audit_user(u)} source={skey} name_hash={_fingerprint(clean)}")
    if len(items) < 2:
        latest = items[-1] if items else None
        return _envelope(
            value=latest["value"] if latest else None,
            unit=latest["unit"] if latest else None,
            ref_low=latest.get("ref_low") if latest else None,
            ref_high=latest.get("ref_high") if latest else None,
            days_stale=_days_since(latest["ts"]) if latest else None,
            source_ids=[it["id"] for it in items if it.get("id") is not None],
            user=u,
            source=skey,
            name=clean,
            count=len(items),
            message="need at least 2 numeric readings to analyze a trend",
        )
    points = [(it["ts"], it["value"]) for it in items]
    values = [it["value"] for it in items]
    ts_list = [it["ts"] for it in items]
    t0 = datetime.fromisoformat(points[0][0])
    xs = [(datetime.fromisoformat(ts) - t0).total_seconds() / 86400.0 for ts, _ in points]
    trend = _linreg_per_day(points)
    linear_r2 = trend.get("r_squared") if trend else None
    last = items[-1]
    latest = {"id": last.get("id"), "ts": last["ts"], "value": round(last["value"], 4),
              "days_stale": _days_since(last["ts"])}
    if "source" in last:
        latest["source"] = last.get("source")
    if "created_ts" in last:
        latest["recorded"] = last.get("created_ts")
    return _envelope(
        value=last["value"],
        unit=last["unit"],
        ref_low=last.get("ref_low"),
        ref_high=last.get("ref_high"),
        days_stale=latest["days_stale"],
        source_ids=[it["id"] for it in items if it.get("id") is not None],
        user=u,
        source=skey,
        name=clean,
        count=len(items),
        window={"since": lo, "until": hi},
        first={"id": items[0].get("id"), "ts": items[0]["ts"], "value": round(items[0]["value"], 4)},
        latest=latest,
        mean=round(statistics.fmean(values), 4),
        median=round(statistics.median(values), 4),
        stdev=round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
        trend=trend,
        baseline=_baseline_frame(points, int(baseline_window_days)),
        rate_of_change=_rate_of_change(points),
        outliers=_mad_outliers(values, ts_list, float(outlier_threshold)),
        shape=_trend_shape(xs, values, linear_r2),
        change_point=_change_point(points),
        disclaimer="Descriptive trend analysis with uncertainty, outlier, baseline, "
                   "shape, and change-point framing; not diagnosis or medical advice.",
    )
