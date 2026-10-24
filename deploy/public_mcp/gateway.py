"""Public OAuth gateway for the budgeting MCP server.

Vendored from tradingo-infra's mcp_gateway role so this deployment is fully
independent of it. The backend is re-exposed at ``<public_url>/<name>/mcp`` as
an OAuth-protected MCP endpoint, which can be added to the Claude app as a
custom connector.

Auth is GitHub OAuth (FastMCP's OAuth proxy). GitHub alone would let any
GitHub user in, so every request's upstream token is also checked against an
allowlist of GitHub logins.

Configuration (environment):
  MCP_GATEWAY_PUBLIC_URL      e.g. https://budgeting-mcp.tailfe5b8d.ts.net
  MCP_GATEWAY_BACKENDS        JSON list of {"name", "url", "headers"?, "header_secrets"?, "bearer_secret"?}
                              header_secrets maps header name -> secret file path;
                              bearer_secret is a file holding a token sent as
                              "Authorization: Bearer <token>".
  MCP_GATEWAY_ALLOWED_USERS   comma-separated GitHub logins
  MCP_GATEWAY_GITHUB_CLIENT_ID / MCP_GATEWAY_GITHUB_CLIENT_SECRET (or *_FILE)
  MCP_GATEWAY_JWT_KEY         (or *_FILE) secret used to sign gateway tokens
  MCP_GATEWAY_PORT            listen port (default 8770)
  FASTMCP_HOME                where client registrations/tokens are persisted
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.server.auth.providers.github import GitHubProvider
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, Mount, Route

log = logging.getLogger("mcp_gateway")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Where an MCP client may be sent back to after login. Claude (web, desktop and
# mobile all use the claude.ai callback) plus localhost for Claude Code.
DEFAULT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
]


@dataclass
class Backend:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    public_url: str
    backends: list[Backend]
    allowed_users: frozenset[str]
    github_client_id: str
    github_client_secret: str
    jwt_key: str
    port: int = 8770
    redirect_uris: list[str] = field(default_factory=lambda: list(DEFAULT_REDIRECT_URIS))


def _env_secret(name: str) -> str:
    """Read NAME, or the file named by NAME_FILE (Docker secrets)."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        return Path(path).read_text().strip()
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"{name} (or {name}_FILE) must be set")
    return value


def parse_backends(raw: str) -> list[Backend]:
    backends = []
    for item in json.loads(raw):
        name = item["name"]
        if not NAME_RE.match(name):
            raise SystemExit(f"invalid backend name {name!r}")
        headers = dict(item.get("headers") or {})
        for header, secret_path in (item.get("header_secrets") or {}).items():
            headers[header] = Path(secret_path).read_text().strip()
        if item.get("bearer_secret"):
            token = Path(item["bearer_secret"]).read_text().strip()
            headers["Authorization"] = f"Bearer {token}"
        backends.append(Backend(name=name, url=item["url"], headers=headers))
    names = [b.name for b in backends]
    if len(set(names)) != len(names):
        raise SystemExit(f"duplicate backend names in {names}")
    if not backends:
        raise SystemExit("MCP_GATEWAY_BACKENDS is empty")
    return backends


def load_config() -> Config:
    users = frozenset(
        u.strip().lower() for u in os.environ.get("MCP_GATEWAY_ALLOWED_USERS", "").split(",") if u.strip()
    )
    if not users:
        raise SystemExit("MCP_GATEWAY_ALLOWED_USERS must list at least one GitHub login")
    public_url = os.environ["MCP_GATEWAY_PUBLIC_URL"].rstrip("/")
    if not public_url.startswith("https://"):
        raise SystemExit("MCP_GATEWAY_PUBLIC_URL must be https")
    return Config(
        public_url=public_url,
        backends=parse_backends(os.environ["MCP_GATEWAY_BACKENDS"]),
        allowed_users=users,
        github_client_id=_env_secret("MCP_GATEWAY_GITHUB_CLIENT_ID"),
        github_client_secret=_env_secret("MCP_GATEWAY_GITHUB_CLIENT_SECRET"),
        jwt_key=_env_secret("MCP_GATEWAY_JWT_KEY"),
        port=int(os.environ.get("MCP_GATEWAY_PORT", "8770")),
    )


class AllowlistVerifier(TokenVerifier):
    """Wraps an upstream verifier and rejects GitHub logins not on the allowlist."""

    def __init__(self, inner: TokenVerifier, allowed_users: frozenset[str]):
        super().__init__(required_scopes=inner.required_scopes)
        self._inner = inner
        self._allowed = allowed_users

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await self._inner.verify_token(token)
        if result is None:
            return None
        login = str((result.claims or {}).get("login") or "").lower()
        if login not in self._allowed:
            log.warning("rejected GitHub user %r (not in allowlist)", login)
            return None
        return result


class AllowlistGitHubProvider(GitHubProvider):
    def __init__(self, *, allowed_users: frozenset[str], **kwargs: Any):
        super().__init__(**kwargs)
        # OAuthProxy re-validates the upstream GitHub token through this
        # verifier on every request (see OAuthProxy.load_access_token), so
        # wrapping it is enough to enforce the allowlist everywhere.
        inner = self._token_validator
        if not isinstance(inner, TokenVerifier):  # guard against FastMCP internals changing
            raise RuntimeError("unexpected FastMCP OAuthProxy internals; refusing to start")
        self._token_validator = AllowlistVerifier(inner, allowed_users)


def backend_key(jwt_key: str, name: str) -> str:
    """Per-backend signing key, so tokens issued for one connector are useless on another."""
    return hmac.new(jwt_key.encode(), name.encode(), hashlib.sha256).hexdigest()


def github_auth(cfg: Config, backend: Backend) -> AuthProvider:
    base = f"{cfg.public_url}/{backend.name}"
    return AllowlistGitHubProvider(
        allowed_users=cfg.allowed_users,
        client_id=cfg.github_client_id,
        client_secret=cfg.github_client_secret,
        base_url=base,
        issuer_url=base,
        # read:user is enough to identify the login; the default ("user")
        # would also grant write access to the GitHub profile.
        required_scopes=["read:user"],
        allowed_client_redirect_uris=cfg.redirect_uris,
        jwt_signing_key=backend_key(cfg.jwt_key, backend.name),
        require_authorization_consent=True,
    )


AuthFactory = Callable[[Config, Backend], AuthProvider | None]


def build_app(cfg: Config, auth_factory: AuthFactory = github_auth) -> Starlette:
    routes: list[BaseRoute] = []
    lifespans = []
    for backend in cfg.backends:
        auth = auth_factory(cfg, backend)
        client = Client(StreamableHttpTransport(backend.url, headers=backend.headers))
        proxy = create_proxy(client, name=f"{backend.name} (via gateway)", auth=auth)
        app = proxy.http_app(path="/mcp")
        if auth is not None:
            # RFC 9728 / RFC 8414 discovery documents live at the host root,
            # with the backend's path appended, e.g.
            # /.well-known/oauth-protected-resource/budgeting/mcp
            routes.extend(auth.get_well_known_routes(mcp_path="/mcp"))
        routes.append(Mount(f"/{backend.name}", app=app))
        lifespans.append(app.router.lifespan_context)
        log.info("serving %s -> %s at %s/%s/mcp", backend.name, backend.url, cfg.public_url, backend.name)

    async def health(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "backends": [b.name for b in cfg.backends]})

    routes.insert(0, Route("/healthz", health))

    @asynccontextmanager
    async def lifespan(app: Starlette):  # each mounted MCP app needs its session manager running
        async with AsyncExitStack() as stack:
            for ctx in lifespans:
                await stack.enter_async_context(ctx(app))
            yield

    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    cfg = load_config()
    log.info("allowed GitHub users: %s", ", ".join(sorted(cfg.allowed_users)))
    uvicorn.run(
        build_app(cfg),
        host="0.0.0.0",
        port=cfg.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
