"""Shared runtime surface for tool modules.

Re-exports every foundation and analysis helper (plus the FastMCP `mcp`
instance) so each tool module can do a single `from healthledger.runtime import *`
instead of a long, brittle list of imports. The re-export is dynamic, so adding a
helper anywhere in the foundation makes it available to every tool automatically.
"""
from __future__ import annotations

from healthledger import config, audit, validation, db, schema, timeutil, guardrails, integrity
from healthledger.analysis import stats, units, series, contract
from healthledger.app import mcp

_FOUNDATION = (config, audit, validation, db, schema, timeutil, guardrails, integrity, stats, units, series, contract)
for _mod in _FOUNDATION:
    for _name, _obj in vars(_mod).items():
        if not _name.startswith("__"):
            globals()[_name] = _obj
globals()["mcp"] = mcp

__all__ = sorted(
    n for n in globals()
    if not n.startswith("__")
    and n not in {"config", "audit", "validation", "db", "schema", "timeutil",
                  "guardrails", "integrity", "stats", "units", "series", "contract",
                  "_FOUNDATION", "_mod", "_name", "_obj"}
)
