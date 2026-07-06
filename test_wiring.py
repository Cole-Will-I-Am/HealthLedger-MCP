#!/usr/bin/env python3
"""Offline connector wiring test.

Verifies OAuth discovery and the unauthenticated 401 contract without real GitHub
credentials or network access.

Run: ./.venv/bin/python test_wiring.py
"""
import json
import os
from urllib.parse import urlparse

# Dummy config so the module builds; fail-closed checks only run in __main__.
# This test exercises the optional remote (http) mode, so pin the transport.
os.environ.setdefault("HEALTH_MCP_TRANSPORT", "http")
os.environ.setdefault("HEALTH_MCP_GITHUB_CLIENT_ID", "test-client-id")
os.environ.setdefault("HEALTH_MCP_GITHUB_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("HEALTH_MCP_ALLOWED_LOGINS", "Cole-Will-I-Am")
os.environ.setdefault("HEALTH_MCP_PUBLIC_URL", "https://health-mcp.manticthink.com")

import server  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

app = server.mcp.http_app(path="/mcp")
failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(name)


def resource_metadata_path(www_auth: str) -> str:
    marker = 'resource_metadata="'
    i = www_auth.find(marker)
    if i < 0:
        return "/.well-known/oauth-protected-resource"
    url = www_auth[i + len(marker):].split('"', 1)[0]
    return urlparse(url).path


with TestClient(app) as client:
    boot = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}},
    )
    www_auth = boot.headers.get("www-authenticate", "")

    prm_path = resource_metadata_path(www_auth)
    r = client.get(prm_path)
    check(f"PRM {prm_path} -> 200", r.status_code == 200, str(r.status_code))
    prm = r.json() if r.status_code == 200 else {}
    check("PRM advertises a resource", bool(prm.get("resource")), json.dumps(prm)[:200])
    check("PRM advertises authorization_servers", bool(prm.get("authorization_servers")))

    r = client.get("/.well-known/oauth-authorization-server")
    check("ASM /.well-known/oauth-authorization-server -> 200", r.status_code == 200, str(r.status_code))
    asm = r.json() if r.status_code == 200 else {}
    check("ASM advertises S256 PKCE", "S256" in (asm.get("code_challenge_methods_supported") or []),
          json.dumps(asm.get("code_challenge_methods_supported")))
    check("ASM has authorization_endpoint", bool(asm.get("authorization_endpoint")))
    check("ASM has token_endpoint", bool(asm.get("token_endpoint")))
    check("ASM supports DCR", bool(asm.get("registration_endpoint")), asm.get("registration_endpoint", "none"))

    r = client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "0"}}},
    )
    check("unauthenticated POST /mcp -> 401", r.status_code == 401, str(r.status_code))
    check("401 carries WWW-Authenticate", "www-authenticate" in {k.lower() for k in r.headers})

print()
print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURE(S): {failures}")
raise SystemExit(1 if failures else 0)
