"""Importing this package registers every tool on the shared FastMCP instance.

Each submodule decorates its functions with `@mcp.tool`, so importing them here
is what wires the whole tool catalog onto the server.
"""
from healthledger.tools import (  # noqa: F401
    core,
    clinical,
    genomics,
    guidance,
    labs,
    life,
    crosssignal,
    trends,
    overview,
    retrieval,
    interop,
)
