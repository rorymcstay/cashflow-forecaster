"""Work around python-engineio's ASGI handler crashing on an aborted request.

engineio.async_drivers.asgi.translate_request awaits `receive()` and, if the
client already disconnected before sending anything, gets an `http.disconnect`
event instead of `http.request`/`websocket.connect` — in that case it returns
`environ = {}` with no further attempt to respond (correctly: there's nobody
left to respond to). But engineio.async_server.AsyncServer.handle_request
then unconditionally does `environ['REQUEST_METHOD']`, raising a bare
KeyError that takes down the whole ASGI call and, from NiceGUI's side, kills
the socket.io connection — which is what was causing "the page keeps
reloading and I lose my place" (a dropped connection that can't resync
forces NiceGUI's outbox to do a full `window.location.reload()`).

This is an upstream bug with no fix as of python-engineio 4.14.0:
https://github.com/miguelgrinberg/python-engineio/issues/464

Since the client is already gone in this case, swallowing the KeyError and
returning is the correct behaviour — there's nothing to send a response to.
Import this module once, before the socket.io server starts handling
requests (i.e. before `ui.run(...)`).
"""

from engineio.async_server import AsyncServer

_original_handle_request = AsyncServer.handle_request


async def _patched_handle_request(self, *args, **kwargs):
    try:
        return await _original_handle_request(self, *args, **kwargs)
    except KeyError as exc:
        if exc.args != ("REQUEST_METHOD",):
            raise
        return None


AsyncServer.handle_request = _patched_handle_request
