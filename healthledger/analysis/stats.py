"""Descriptive statistics: regression with uncertainty, correlation, robust
outliers, and change-points. Pure functions, no I/O."""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta


def _ols(xs: list[float], ys: list[float]) -> dict | None:
    """Ordinary least squares of ys on xs, with slope uncertainty.

    Returns slope, intercept, R^2 and — when n>2 — the slope's standard error,
    two-sided p-value (H0: slope=0) and 95% confidence interval, so a real trend
    can be told apart from noise. None if the regressor has no spread. (Depends
    on _t_two_sided_p / _t_crit, resolved at call time.)"""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    sse = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    fit = {
        "slope": slope,
        "intercept": intercept,
        "r_squared": (1.0 - sse / syy) if syy > 0 else None,
        "sse": sse,
        "n": n,
    }
    if n > 2:
        s2 = sse / (n - 2)
        se = math.sqrt(s2 / sxx) if s2 > 0 else 0.0
        fit["slope_stderr"] = se
        if se > 0:
            t = slope / se
            fit["slope_t"] = t
            fit["slope_p_value"] = _t_two_sided_p(t, n - 2)
            tc = _t_crit(n - 2)
            if tc is not None:
                fit["slope_ci95"] = [slope - tc * se, slope + tc * se]
        else:
            fit["slope_p_value"] = 0.0 if slope != 0 else None
            fit["slope_ci95"] = [slope, slope]
    return fit


def _t_crit(df: float, alpha: float = 0.05) -> float | None:
    """Two-sided critical t value: the |t| whose two-sided p equals alpha.
    Found by bisection on _t_two_sided_p (monotone-decreasing in |t|)."""
    if df <= 0:
        return None
    lo, hi = 0.0, 1000.0
    for _ in range(64):
        mid = (lo + hi) / 2.0
        if (_t_two_sided_p(mid, df) or 0.0) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _quartiles(values: list[float]) -> list[float] | None:
    """[Q1, Q3] via linear interpolation between order statistics."""
    s = sorted(values)
    n = len(s)
    if n < 2:
        return None

    def _pct(p: float) -> float:
        idx = p * (n - 1)
        lo = int(idx)
        frac = idx - lo
        return s[lo] if lo + 1 >= n else s[lo] * (1 - frac) + s[lo + 1] * frac

    return [_pct(0.25), _pct(0.75)]


def _linreg_per_day(points: list[tuple[str, float]]) -> dict | None:
    """Least-squares slope of value vs. time (in days since the first sample),
    now carrying slope uncertainty so 'trending up' can be told from noise."""
    if len(points) < 2:
        return None
    t0 = datetime.fromisoformat(points[0][0])
    xs = [(datetime.fromisoformat(ts) - t0).total_seconds() / 86400.0 for ts, _ in points]
    ys = [v for _, v in points]
    fit = _ols(xs, ys)
    if fit is None:
        return None
    slope = fit["slope"]
    intercept = fit["intercept"]
    span_days = xs[-1] - xs[0]
    result = {
        "slope_per_day": round(slope, 6),
        "change_over_span": round(slope * span_days, 4),
        "span_days": round(span_days, 3),
        "direction": "rising" if slope > 0 else ("falling" if slope < 0 else "flat"),
        "projected_next_day": round(intercept + slope * (xs[-1] + 1), 4),
        "r_squared": round(fit["r_squared"], 4) if fit["r_squared"] is not None else None,
    }
    if "slope_stderr" in fit:
        result["slope_stderr"] = round(fit["slope_stderr"], 6)
        ci = fit.get("slope_ci95")
        if ci is not None:
            result["slope_ci95_per_day"] = [round(ci[0], 6), round(ci[1], 6)]
        p = fit.get("slope_p_value")
        if p is not None:
            result["slope_p_value"] = p
            result["significant"] = p < 0.05
        if ci is not None:
            result["confidence"] = (
                "slope is distinguishable from flat (95% CI excludes zero)"
                if (ci[0] > 0 or ci[1] < 0) else
                "slope is NOT distinguishable from flat (95% CI includes zero) — treat as noise"
            )
    return result

def _rankdata(values: list[float]) -> list[float]:
    """Average ranks (ties share the mean rank), 1-based — for Spearman."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson_r(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Numerical Recipes)."""
    fpmin = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_two_sided_p(t: float, df: float) -> float | None:
    """Two-sided p-value for a Student-t statistic with df degrees of freedom."""
    if df <= 0:
        return None
    if not math.isfinite(t):
        return 0.0
    return round(_betai(df / 2.0, 0.5, df / (df + t * t)), 6)


def _corr_p_value(r: float, n: int) -> float | None:
    df = n - 2
    if df <= 0:
        return None
    if abs(r) >= 1.0:
        return 0.0
    t = r * math.sqrt(df / (1.0 - r * r))
    return _t_two_sided_p(t, df)


def _corr_strength(r: float) -> str:
    a = abs(r)
    if a < 0.1:
        return "negligible"
    if a < 0.3:
        return "weak"
    if a < 0.5:
        return "moderate"
    if a < 0.7:
        return "strong"
    return "very strong"


def _group_stats(items: list[dict]) -> dict:
    values = [it["value"] for it in items]
    n = len(values)
    if n == 0:
        return {"count": 0}
    stats = {
        "count": n,
        "first_ts": items[0]["ts"],
        "last_ts": items[-1]["ts"],
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "stdev": round(statistics.stdev(values), 4) if n > 1 else 0.0,
        "trend": _linreg_per_day([(it["ts"], it["value"]) for it in items]) if n >= 2 else None,
    }
    return stats


def _welch_t(a_vals: list[float], b_vals: list[float]) -> dict | None:
    """Welch's unequal-variance t-test for a difference in means."""
    na, nb = len(a_vals), len(b_vals)
    if na < 2 or nb < 2:
        return None
    va, vb = statistics.variance(a_vals), statistics.variance(b_vals)
    se2 = va / na + vb / nb
    if se2 <= 0:
        return None
    t = (statistics.fmean(b_vals) - statistics.fmean(a_vals)) / math.sqrt(se2)
    df_den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    df = (se2 ** 2) / df_den if df_den > 0 else float(na + nb - 2)
    return {"t": round(t, 4), "df": round(df, 2), "p_value": _t_two_sided_p(t, df)}

def _det3(m: list[list[float]]) -> float:
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def _quad_r2(xs: list[float], ys: list[float]) -> float | None:
    """R^2 of a quadratic least-squares fit (normal equations via Cramer's rule),
    used only to detect curvature a straight line would miss."""
    n = len(xs)
    if n < 4:
        return None
    s1 = sum(xs)
    s2 = sum(x * x for x in xs)
    s3 = sum(x ** 3 for x in xs)
    s4 = sum(x ** 4 for x in xs)
    sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sx2y = sum(x * x * y for x, y in zip(xs, ys))
    base = [[s4, s3, s2], [s3, s2, s1], [s2, s1, float(n)]]
    det = _det3(base)
    if det == 0:
        return None
    rhs = [sx2y, sxy, sy]
    a = _det3([[rhs[0], s3, s2], [rhs[1], s2, s1], [rhs[2], s1, float(n)]]) / det
    b = _det3([[s4, rhs[0], s2], [s3, rhs[1], s1], [s2, rhs[2], float(n)]]) / det
    c = _det3([[s4, s3, rhs[0]], [s3, s2, rhs[1]], [s2, s1, rhs[2]]]) / det
    my = sy / n
    syy = sum((y - my) ** 2 for y in ys)
    if syy == 0:
        return None
    sse = sum((y - (a * x * x + b * x + c)) ** 2 for x, y in zip(xs, ys))
    return 1.0 - sse / syy


def _mad_outliers(values: list[float], ts_list: list[str], thresh: float = 3.5) -> dict:
    """Flag points by modified z-score (median/MAD) — robust to the outliers
    themselves, unlike a mean/stdev rule that they distort."""
    n = len(values)
    if n < 4:
        return {"method": "modified z-score (median/MAD)", "threshold": thresh,
                "count": 0, "points": [], "note": "need >=4 points to judge outliers"}
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    if mad > 0:
        scores = [0.6745 * (v - med) / mad for v in values]
        method = "modified z-score (median/MAD)"
    else:
        sd = statistics.pstdev(values)
        if sd == 0:
            return {"method": "modified z-score (median/MAD)", "threshold": thresh,
                    "count": 0, "points": [], "note": "no variance"}
        scores = [(v - med) / sd for v in values]
        method = "z-score (MAD=0, fell back to stdev)"
    pts = [{"ts": ts_list[i], "value": round(values[i], 4), "robust_z": round(scores[i], 2)}
           for i in range(n) if abs(scores[i]) > thresh]
    pts.sort(key=lambda p: abs(p["robust_z"]), reverse=True)
    return {"method": method, "threshold": thresh, "count": len(pts), "points": pts[:20]}


def _baseline_frame(points: list[tuple[str, float]], lookback_days: int) -> dict:
    """Latest reading vs the user's own recent median and typical range — how a
    clinician reads a value, rather than latest-vs-fitted-line."""
    latest_ts, latest_val = points[-1]
    cutoff = datetime.fromisoformat(latest_ts) - timedelta(days=max(1, lookback_days))
    vals = [v for ts, v in points if datetime.fromisoformat(ts) >= cutoff]
    if len(vals) < 2:
        return {"lookback_days": lookback_days, "n": len(vals),
                "note": "not enough history in the baseline window"}
    med = statistics.median(vals)
    q = _quartiles(vals)
    mad = statistics.median([abs(v - med) for v in vals])
    delta = latest_val - med
    frame = {
        "lookback_days": lookback_days,
        "n": len(vals),
        "median": round(med, 4),
        "iqr": [round(q[0], 4), round(q[1], 4)] if q else None,
        "latest": {"ts": latest_ts, "value": round(latest_val, 4)},
        "delta_vs_median": round(delta, 4),
        "percent_vs_median": round(delta / med * 100.0, 2) if med != 0 else None,
        "robust_z": round(0.6745 * delta / mad, 2) if mad > 0 else None,
    }
    if q:
        frame["position"] = ("above your typical range (>Q3)" if latest_val > q[1]
                             else "below your typical range (<Q1)" if latest_val < q[0]
                             else "within your typical range (Q1-Q3)")
    return frame


def _rate_of_change(points: list[tuple[str, float]]) -> dict:
    """Recent vs earlier slope, to expose acceleration a single global line hides."""
    n = len(points)
    if n < 4:
        return {"note": "need >=4 points to compare recent vs earlier rate"}

    def _seg_slope(seg):
        if len(seg) < 2:
            return None
        t0 = datetime.fromisoformat(seg[0][0])
        xs = [(datetime.fromisoformat(ts) - t0).total_seconds() / 86400.0 for ts, _ in seg]
        fit = _ols(xs, [v for _, v in seg])
        return fit["slope"] if fit else None

    mid = n // 2
    earlier = _seg_slope(points[:mid + 1])
    recent = _seg_slope(points[mid:])
    out = {
        "earlier_slope_per_day": round(earlier, 6) if earlier is not None else None,
        "recent_slope_per_day": round(recent, 6) if recent is not None else None,
    }
    if earlier is not None and recent is not None:
        out["acceleration_per_day"] = round(recent - earlier, 6)
        out["recent_change_per_30d"] = round(recent * 30.0, 4)
        out["direction_shift"] = (earlier <= 0 < recent) or (earlier >= 0 > recent)
    return out


def _trend_shape(xs: list[float], ys: list[float], linear_r2: float | None) -> dict:
    """Warn when a straight line is the wrong model: weak fit, residual runs
    (unmodeled structure), or a materially better quadratic (curvature)."""
    n = len(xs)
    warnings: list[str] = []
    advised = True
    fit = _ols(xs, ys)
    runs = None
    if fit and n >= 8:
        resid = [y - (fit["intercept"] + fit["slope"] * x) for x, y in zip(xs, ys)]
        signs = [1 if r >= 0 else -1 for r in resid if r != 0]
        n1 = sum(1 for s in signs if s > 0)
        n2 = sum(1 for s in signs if s < 0)
        if n1 > 0 and n2 > 0:
            obs = 1 + sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])
            exp = 1 + 2.0 * n1 * n2 / (n1 + n2)
            var = (2.0 * n1 * n2 * (2.0 * n1 * n2 - n1 - n2)) / (((n1 + n2) ** 2) * (n1 + n2 - 1))
            z = (obs - exp) / math.sqrt(var) if var > 0 else 0.0
            runs = {"observed_runs": obs, "expected_runs": round(exp, 2), "z": round(z, 2)}
            if z < -1.96:
                warnings.append("residuals cluster in runs (z<-1.96): the signal has structure "
                                "a straight line misses — consider a curve or cycle")
                advised = False
    curvature = None
    quad_r2 = _quad_r2(xs, ys)
    if quad_r2 is not None and linear_r2 is not None:
        gain = quad_r2 - linear_r2
        curvature = {"linear_r2": round(linear_r2, 4), "quadratic_r2": round(quad_r2, 4),
                     "r2_gain": round(gain, 4), "curved": gain > 0.1}
        if gain > 0.1:
            warnings.append("a quadratic fits materially better (R2 gain>0.10): the trend is "
                            "likely non-linear, so linear extrapolation may mislead")
            advised = False
    if linear_r2 is not None and linear_r2 < 0.3:
        warnings.append("weak linear fit (R2<0.30): the straight-line slope explains little "
                        "of the variation")
        advised = False
    return {"linear_r2": round(linear_r2, 4) if linear_r2 is not None else None,
            "residual_runs_test": runs, "curvature": curvature,
            "linear_extrapolation_advised": advised, "warnings": warnings}


def _change_point(points: list[tuple[str, float]], min_seg: int = 3,
                  min_reduction: float = 0.3) -> dict:
    """Single change-point by the split that best reduces within-segment variance
    (piecewise-constant mean). Heuristic and descriptive, not a formal test."""
    ys = [v for _, v in points]
    n = len(ys)
    if n < 2 * min_seg:
        return {"detected": False, "note": f"need >={2 * min_seg} points"}

    def _sse(seg):
        if not seg:
            return 0.0
        m = sum(seg) / len(seg)
        return sum((v - m) ** 2 for v in seg)

    total = _sse(ys)
    if total == 0:
        return {"detected": False, "note": "no variance"}
    best_k, best_sse = None, None
    for k in range(min_seg, n - min_seg + 1):
        s = _sse(ys[:k]) + _sse(ys[k:])
        if best_sse is None or s < best_sse:
            best_k, best_sse = k, s
    reduction = 1.0 - best_sse / total
    if reduction < min_reduction:
        return {"detected": False, "best_variance_reduction": round(reduction, 3),
                "note": "no clear regime change"}
    left, right = ys[:best_k], ys[best_k:]
    return {
        "detected": True,
        "date": points[best_k][0],
        "index": best_k,
        "variance_reduction": round(reduction, 3),
        "before": {"n": len(left), "mean": round(statistics.fmean(left), 4)},
        "after": {"n": len(right), "mean": round(statistics.fmean(right), 4)},
        "mean_shift": round(statistics.fmean(right) - statistics.fmean(left), 4),
        "note": f"heuristic single change-point (variance-reduction >= {min_reduction}); descriptive only",
    }
