"""GitHub OAuth allow-list verifier and OAuth proxy (remote http mode only)."""
from __future__ import annotations

from healthledger.config import *  # noqa: F401,F403
from healthledger.audit import _audit
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier


class AllowlistGitHubTokenVerifier(GitHubTokenVerifier):
    """GitHub token verifier restricted to an allow-list of logins (defence in depth):
    GitHub validates the token, then the resolved `login` must be in ALLOWED_LOGINS."""

    def __init__(self, *, allowed_logins: set[str], **kwargs):
        super().__init__(**kwargs)
        self._allowed_logins = {l.lower() for l in allowed_logins}

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await super().verify_token(token)
        if result is None:
            return None
        login = (result.claims or {}).get("login")
        if not login or login.lower() not in self._allowed_logins:
            _audit("auth.denied", f"login={login!r} not in allow-list")
            return None
        return result


def build_auth() -> OAuthProxy:
    verifier = AllowlistGitHubTokenVerifier(
        allowed_logins=ALLOWED_LOGINS,
        required_scopes=["user"],
        cache_ttl_seconds=300,
    )
    return OAuthProxy(
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id=CLIENT_ID,
        upstream_client_secret=CLIENT_SECRET,
        token_verifier=verifier,
        base_url=PUBLIC_URL,
        redirect_path="/auth/callback",
        issuer_url=PUBLIC_URL,
        require_authorization_consent=True,
    )


# --------------------------------------------------------------------------- server
