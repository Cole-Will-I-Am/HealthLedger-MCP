"""Server entry point: registers all tools and runs the MCP server (local
stdio by default, optional remote http)."""
from __future__ import annotations

import sys

from healthledger.config import (
    TRANSPORT, DB_PATH, DEFAULT_USER, HOST, PORT, MCP_PATH, PUBLIC_URL,
    ALLOWED_LOGINS, _fail_closed,
)
from healthledger.schema import _init_db
from healthledger.app import mcp
from healthledger import tools  # noqa: F401  (importing registers every tool)


def main() -> None:
    """Console entry point. Runs a local stdio server by default; HEALTH_MCP_TRANSPORT=http
    runs the optional remote, OAuth-protected server instead."""
    if TRANSPORT == "stdio":
        # Local mode: any MCP client launches this as a trusted subprocess. No OAuth,
        # no network — the record stays on this machine.
        _init_db()
        sys.stderr.write(
            f"HealthLedger MCP starting (stdio, local): db={DB_PATH} default_user={DEFAULT_USER}\n"
        )
        mcp.run()  # stdio transport (the FastMCP default)
    else:
        _fail_closed()
        _init_db()
        sys.stderr.write(
            f"HealthLedger MCP starting (http, remote): {PUBLIC_URL}{MCP_PATH} -> {HOST}:{PORT} "
            f"(db={DB_PATH}, allow-list: {', '.join(sorted(ALLOWED_LOGINS))})\n"
        )
        mcp.run(transport="http", host=HOST, port=PORT, path=MCP_PATH)


if __name__ == "__main__":
    main()
