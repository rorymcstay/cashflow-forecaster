"""HTTP side of multi-device sync (the merge logic itself is app/sync.py).

One device shows a pairing code — a QR of {"url", "token"} — and serves
/api/sync/pull + /api/sync/push for as long as that token is valid. The
other device scans it and drives the whole exchange from its own server
process: pull the peer's new log entries and merge them, then push back
whatever the peer hasn't seen. Tokens live only in this process's memory and
expire after PAIRING_TTL_SECONDS, so a code that's been photographed or left
on screen stops working on its own.
"""

import json
import secrets
import time
from dataclasses import asdict

import httpx
from fastapi import HTTPException
from nicegui import app, run
from pydantic import BaseModel

from app import sync
from app.db import get_session

PAIRING_TTL_SECONDS = 600

_pairing_tokens: dict[str, float] = {}  # token -> expiry (epoch seconds)


def create_pairing_code(base_url: str) -> tuple[str, float]:
    """A fresh pairing code for this device, and when it expires."""
    now = time.time()
    for token in [t for t, expiry in _pairing_tokens.items() if expiry < now]:
        del _pairing_tokens[token]
    token = secrets.token_urlsafe(16)
    expiry = now + PAIRING_TTL_SECONDS
    _pairing_tokens[token] = expiry
    return json.dumps({"url": base_url.rstrip("/"), "token": token}, separators=(",", ":")), expiry


def _check_request(token: str, protocol: int) -> None:
    if _pairing_tokens.get(token, 0) < time.time():
        raise HTTPException(403, "Pairing code expired or unknown — show a new one and scan again.")
    if protocol != sync.PROTOCOL_VERSION:
        raise HTTPException(409, "The two devices are running incompatible app versions — update both.")


class PullRequest(BaseModel):
    token: str
    protocol: int
    seen: dict[str, int]


class PushRequest(BaseModel):
    token: str
    protocol: int
    entries: list[dict]


# Plain (non-async) handlers: FastAPI runs them in a worker thread, keeping
# the DB work off the event loop every browser tab shares.
@app.post("/api/sync/pull")
def pull(request: PullRequest) -> dict:
    _check_request(request.token, request.protocol)
    session = get_session()
    try:
        return {
            "device_id": sync.device_id(session.connection()),
            "entries": sync.entries_since(session, request.seen),
            "seen": sync.version_vector(session),
        }
    finally:
        session.close()


@app.post("/api/sync/push")
def push(request: PushRequest) -> dict:
    _check_request(request.token, request.protocol)
    session = get_session()
    try:
        result = sync.merge_entries(session, request.entries)
        session.commit()
        return asdict(result)
    finally:
        session.close()


class SyncError(Exception):
    pass


def _parse_pairing_code(code: str) -> tuple[str, str]:
    try:
        payload = json.loads(code)
        return payload["url"].rstrip("/"), payload["token"]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SyncError("That isn't a sync pairing code.") from None


async def _in_thread(fn, *args):
    result = await run.io_bound(fn, *args)
    if result is None:
        raise SyncError("The app is shutting down.")
    return result


def _local_state() -> tuple[str, dict[str, int]]:
    session = get_session()
    try:
        device = sync.device_id(session.connection())
        session.commit()  # in case device_id() just created it
        return device, sync.version_vector(session)
    finally:
        session.close()


def _merge(entries: list[dict]) -> sync.MergeResult:
    session = get_session()
    try:
        result = sync.merge_entries(session, entries)
        session.commit()
        return result
    finally:
        session.close()


def _entries_since(seen: dict[str, int]) -> list[dict]:
    session = get_session()
    try:
        return sync.entries_since(session, seen)
    finally:
        session.close()


async def sync_with_peer(code: str) -> tuple[sync.MergeResult, sync.MergeResult]:
    """Two-way sync with the device that showed `code`. Returns (what this
    device received, what the peer received)."""
    url, token = _parse_pairing_code(code)
    device, seen = await _in_thread(_local_state)
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{url}/api/sync/pull",
                json={"token": token, "protocol": sync.PROTOCOL_VERSION, "seen": seen},
            )
            _raise_for_status(response)
            body = response.json()
            if body["device_id"] == device:
                raise SyncError("That's this device's own pairing code — scan it from the other device.")
            received = await _in_thread(_merge, body["entries"])

            outgoing = await _in_thread(_entries_since, body["seen"])
            response = await client.post(
                f"{url}/api/sync/push",
                json={"token": token, "protocol": sync.PROTOCOL_VERSION, "entries": outgoing},
            )
            _raise_for_status(response)
            sent = sync.MergeResult(**response.json())
    except httpx.HTTPError as exc:
        raise SyncError(f"Couldn't reach the other device at {url}: {exc}") from exc
    return received, sent


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        detail = response.json()["detail"]
    except (ValueError, KeyError, TypeError):
        detail = f"HTTP {response.status_code}"
    raise SyncError(str(detail))
