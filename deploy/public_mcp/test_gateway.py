"""Tests for gateway.py. Run from this directory:
uv run --with-requirements requirements.txt --with pytest --with httpx pytest -q
"""

from __future__ import annotations

import asyncio
import re
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.server.auth import AccessToken, TokenVerifier

import gateway

PUBLIC = "https://gw.example.test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


@pytest.fixture(scope="module")
def backend_url():
    mcp = FastMCP("dummy")

    @mcp.tool
    def echo(text: str) -> str:
        return f"echo:{text}"

    port = _free_port()
    server = _serve(mcp.http_app(path="/mcp"), port)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True


def _cfg(backend_url: str) -> gateway.Config:
    return gateway.Config(
        public_url=PUBLIC,
        backends=[gateway.Backend("budgeting", backend_url), gateway.Backend("other", backend_url)],
        allowed_users=frozenset({"rorymcstay"}),
        github_client_id="test-client-id",
        github_client_secret="test-client-secret",
        jwt_key="x" * 32,
    )


@pytest.fixture(scope="module")
def monkeypatch_module(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("FASTMCP_HOME", str(tmp_path_factory.mktemp("fastmcp-home")))
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def authed_gateway(backend_url, monkeypatch_module):
    port = _free_port()
    server = _serve(gateway.build_app(_cfg(backend_url)), port)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def test_healthz(authed_gateway):
    r = httpx.get(f"{authed_gateway}/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_unauthenticated_request_is_rejected_with_discovery_hint(authed_gateway):
    r = httpx.post(f"{authed_gateway}/budgeting/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    www = r.headers["www-authenticate"]
    assert f"{PUBLIC}/.well-known/oauth-protected-resource/budgeting/mcp" in www


def test_discovery_chain(authed_gateway):
    prm = httpx.get(f"{authed_gateway}/.well-known/oauth-protected-resource/budgeting/mcp")
    assert prm.status_code == 200, prm.text
    prm = prm.json()
    assert prm["resource"] == f"{PUBLIC}/budgeting/mcp"
    assert prm["authorization_servers"] == [f"{PUBLIC}/budgeting"]

    asm = httpx.get(f"{authed_gateway}/.well-known/oauth-authorization-server/budgeting")
    assert asm.status_code == 200, asm.text
    asm = asm.json()
    assert asm["issuer"] == f"{PUBLIC}/budgeting"
    assert asm["authorization_endpoint"] == f"{PUBLIC}/budgeting/authorize"
    assert asm["token_endpoint"] == f"{PUBLIC}/budgeting/token"
    assert asm["registration_endpoint"] == f"{PUBLIC}/budgeting/register"

    # Each backend has its own, separate authorization server.
    other = httpx.get(f"{authed_gateway}/.well-known/oauth-protected-resource/other/mcp").json()
    assert other["authorization_servers"] == [f"{PUBLIC}/other"]


def _register(base: str, redirect_uri: str) -> httpx.Response:
    return httpx.post(
        f"{base}/budgeting/register",
        json={
            "redirect_uris": [redirect_uri],
            "client_name": "test",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
    )


def test_claude_can_register_and_is_sent_to_consent(authed_gateway):
    r = _register(authed_gateway, "https://claude.ai/api/mcp/auth_callback")
    assert r.status_code == 201, r.text
    client_id = r.json()["client_id"]

    r = httpx.get(
        f"{authed_gateway}/budgeting/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
            "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            "code_challenge_method": "S256",
            "state": "abc",
            "resource": f"{PUBLIC}/budgeting/mcp",
        },
    )
    # Consent page first (or a redirect to it) - never straight to GitHub.
    assert r.status_code in (200, 302, 303, 307), r.text
    if r.status_code != 200:
        assert "github.com" not in r.headers["location"]


def test_foreign_redirect_uri_is_refused(authed_gateway):
    r = _register(authed_gateway, "https://evil.example.com/callback")
    assert r.status_code == 400, r.text


def test_upstream_redirect_uri_is_under_public_url(backend_url, monkeypatch_module):
    provider = gateway.github_auth(_cfg(backend_url), gateway.Backend("budgeting", backend_url))
    assert str(provider.base_url).rstrip("/") == f"{PUBLIC}/budgeting"
    # GitHub needs the OAuth app's callback URL to be a parent of this.
    redirect = f"{str(provider.base_url).rstrip('/')}{provider._redirect_path}"
    assert redirect == f"{PUBLIC}/budgeting/auth/callback"


class _FakeVerifier(TokenVerifier):
    def __init__(self, login: str | None):
        super().__init__(required_scopes=["read:user"])
        self.login = login

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.login is None:
            return None
        return AccessToken(token=token, client_id="1", scopes=["read:user"], claims={"login": self.login})


@pytest.mark.parametrize(
    "login,allowed",
    [("rorymcstay", True), ("RoryMcStay", True), ("someone-else", False), (None, False), ("", False)],
)
def test_allowlist(login, allowed):
    v = gateway.AllowlistVerifier(_FakeVerifier(login), frozenset({"rorymcstay"}))
    result = asyncio.run(v.verify_token("t"))
    assert (result is not None) is allowed


def test_provider_wraps_validator(backend_url, monkeypatch_module):
    provider = gateway.github_auth(_cfg(backend_url), gateway.Backend("budgeting", backend_url))
    assert isinstance(provider._token_validator, gateway.AllowlistVerifier)


def test_tokens_are_scoped_per_backend():
    assert gateway.backend_key("k", "budgeting") != gateway.backend_key("k", "other")


def test_proxy_forwards_tools(backend_url):
    port = _free_port()
    server = _serve(gateway.build_app(_cfg(backend_url), auth_factory=lambda c, b: None), port)
    try:

        async def go():
            async with Client(f"http://127.0.0.1:{port}/other/mcp") as c:
                tools = [t.name for t in await c.list_tools()]
                res = await c.call_tool("echo", {"text": "hi"})
                return tools, res

        tools, res = asyncio.run(go())
        assert tools == ["echo"]
        assert res.data == "echo:hi"
    finally:
        server.should_exit = True


def test_parse_backends_reads_header_secrets(tmp_path):
    secret = tmp_path / "tok"
    secret.write_text("s3cret\n")
    [b] = gateway.parse_backends(
        f'[{{"name": "docker", "url": "http://x/mcp/", "header_secrets": {{"Authorization": "{secret}"}}}}]'
    )
    assert b.headers == {"Authorization": "s3cret"}

    [b] = gateway.parse_backends(
        f'[{{"name": "docker", "url": "http://x/mcp/", "bearer_secret": "{secret}"}}]'
    )
    assert b.headers == {"Authorization": "Bearer s3cret"}


@pytest.mark.parametrize(
    "raw", ['[{"name": "Bad Name", "url": "u"}]', "[]", '[{"name":"a","url":"u"},{"name":"a","url":"u"}]']
)
def test_parse_backends_rejects_bad_config(raw):
    with pytest.raises(SystemExit):
        gateway.parse_backends(raw)


def test_authorize_redirects_to_github_with_public_callback(authed_gateway):
    """After consent, the user is sent to GitHub with our public callback URL."""
    r = _register(authed_gateway, "http://localhost:5555/cb")
    client_id = r.json()["client_id"]
    with httpx.Client(base_url=authed_gateway, follow_redirects=False) as h:
        r = h.get(
            "/budgeting/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "http://localhost:5555/cb",
                "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
                "code_challenge_method": "S256",
                "state": "abc",
            },
        )
        # Walk through the consent page by approving it if present.
        hops = 0
        while r.status_code in (302, 303, 307) and "github.com" not in r.headers["location"] and hops < 5:
            loc = r.headers["location"].replace(PUBLIC, "")
            r = h.get(loc)
            hops += 1
        if r.status_code == 200:
            html = r.text
            assert 'id="consentForm"' in html, html[:500]
            fields = dict(re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html))
            fields["action"] = "approve"
            # The form posts back to the page's own URL (action="").
            # Cookies are Secure (public URL is https); this test talks plain
            # http, so pass the consent cookie back by hand.
            cookie = "; ".join(c.split(";", 1)[0] for c in r.headers.get_list("set-cookie"))
            r = h.post(str(r.url), data=fields, headers={"cookie": cookie})
        assert r.status_code in (302, 303, 307), r.text[:500]
        loc = urlparse(r.headers["location"])
        assert loc.netloc == "github.com"
        q = parse_qs(loc.query)
        assert q["redirect_uri"] == [f"{PUBLIC}/budgeting/auth/callback"]
        assert q["client_id"] == ["test-client-id"]
        assert "read:user" in q["scope"][0]
