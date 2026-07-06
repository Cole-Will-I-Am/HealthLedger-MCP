"""Unit and reference-range normalization."""
from __future__ import annotations

import math


# --- unit + reference normalization -----------------------------------------
_UNIT_ALIASES = {
    "mgdl": "mg/dl", "mg/dl": "mg/dl", "mg/dL": "mg/dl",
    "gdl": "g/dl", "g/dl": "g/dl", "gl": "g/l", "g/l": "g/l",
    "mgl": "mg/l", "mg/l": "mg/l",
    "ug/ml": "ug/ml", "mcg/ml": "ug/ml", "ug/dl": "ug/dl", "mcg/dl": "ug/dl",
    "ug/l": "ug/l", "mcg/l": "ug/l", "ng/ml": "ng/ml", "ng/dl": "ng/dl", "pg/ml": "pg/ml",
    "mmol/l": "mmol/l", "mmoll": "mmol/l", "umol/l": "umol/l", "mcmol/l": "umol/l",
    "nmol/l": "nmol/l", "pmol/l": "pmol/l", "mol/l": "mol/l",
    "percent": "%", "%": "%", "pct": "%",
    "kg": "kg", "kgs": "kg", "g": "g", "gram": "g", "grams": "g",
    "mg": "mg", "ug": "ug", "mcg": "ug", "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "cm": "cm", "m": "m", "mm": "mm", "in": "in", "inch": "in", "inches": "in", "ft": "ft",
    "l": "l", "liter": "l", "litre": "l", "dl": "dl", "ml": "ml",
    "c": "c", "celsius": "c", "centigrade": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k",
    "bpm": "bpm", "mmhg": "mmhg", "count": "count", "steps": "count",
}

# linear-conversion families: unit -> multiplier to the family's base unit.
_DIMENSIONS = {
    "mass": {"kg": 1000.0, "g": 1.0, "mg": 1e-3, "ug": 1e-6, "lb": 453.59237, "oz": 28.349523125},
    "length": {"m": 1.0, "cm": 0.01, "mm": 1e-3, "in": 0.0254, "ft": 0.3048},
    "volume": {"l": 1.0, "dl": 0.1, "ml": 1e-3},
    # mass concentration, base g/L
    "mass_conc": {"g/l": 1.0, "g/dl": 10.0, "mg/dl": 0.01, "mg/l": 1e-3,
                  "ug/ml": 1e-3, "ug/dl": 1e-5, "ug/l": 1e-6,
                  "ng/ml": 1e-6, "ng/dl": 1e-8, "pg/ml": 1e-9},
    # molar concentration, base mol/L
    "molar_conc": {"mol/l": 1.0, "mmol/l": 1e-3, "umol/l": 1e-6, "nmol/l": 1e-9, "pmol/l": 1e-12},
}
_MASS_CONC = _DIMENSIONS["mass_conc"]
_MOLAR_CONC = _DIMENSIONS["molar_conc"]

# molar masses (g/mol) that enable mass<->molar concentration bridging per analyte.
_ANALYTE_MW = {
    "glucose": 180.16,
    "cholesterol": 386.65, "total_cholesterol": 386.65,
    "hdl": 386.65, "hdl_cholesterol": 386.65, "ldl": 386.65, "ldl_cholesterol": 386.65,
    "triglycerides": 885.4, "triglyceride": 885.4,
    "creatinine": 113.12, "uric_acid": 168.11, "calcium": 40.08,
    "urea": 60.06, "bilirubin": 584.66, "total_bilirubin": 584.66,
    "testosterone": 288.42, "cortisol": 362.46,
}
_TEMP_UNITS = ("c", "f", "k")


def _norm_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    u = str(unit).strip().lower().replace("µ", "u").replace("μ", "u").replace("°", "")
    u = u.replace(" ", "")
    if not u:
        return None
    return _UNIT_ALIASES.get(u, u)


def _temp_convert(value: float, frm: str, to: str) -> float | None:
    if frm == "c":
        c = value
    elif frm == "f":
        c = (value - 32.0) * 5.0 / 9.0
    elif frm == "k":
        c = value - 273.15
    else:
        return None
    return {"c": c, "f": c * 9.0 / 5.0 + 32.0, "k": c + 273.15}.get(to)


def _convert_value(value: float | None, from_unit: str | None, to_unit: str | None,
                   analyte: str | None = None) -> tuple[float | None, bool, str]:
    """Convert a value between units. Returns (converted, ok, method_tag).

    Handles same-unit identity, temperature, linear families (mass, length,
    volume, mass/molar concentration), and analyte-aware mass<->molar bridging.
    Never guesses: an unknown pairing returns (None, False, reason)."""
    if value is None:
        return None, False, "no_value"
    frm, to = _norm_unit(from_unit), _norm_unit(to_unit)
    if frm == to:
        return value, True, "identity"
    if frm is None or to is None:
        return None, False, "unknown_unit"
    if frm in _TEMP_UNITS and to in _TEMP_UNITS:
        r = _temp_convert(value, frm, to)
        return (r, r is not None, "temperature")
    for dim, table in _DIMENSIONS.items():
        if frm in table and to in table:
            return value * table[frm] / table[to], True, dim
    mw = _ANALYTE_MW.get(analyte) if analyte else None
    if mw:
        if frm in _MASS_CONC and to in _MOLAR_CONC:
            mol_per_l = (value * _MASS_CONC[frm]) / mw
            return mol_per_l / _MOLAR_CONC[to], True, "molar_from_mass"
        if frm in _MOLAR_CONC and to in _MASS_CONC:
            g_per_l = (value * _MOLAR_CONC[frm]) * mw
            return g_per_l / _MASS_CONC[to], True, "mass_from_molar"
