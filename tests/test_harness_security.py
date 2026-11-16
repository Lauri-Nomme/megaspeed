"""The eval/screenshot bridge must not be drivable by an arbitrary client (SEC-2).

``POST /api/eval`` runs arbitrary JS in the attached browser (which has
``fetch`` and the whole store behind it) and ``POST /api/screenshot`` writes a
file, so the bridge is the one place where the "read-only UI cannot leak"
invariant could break. These tests drive the real ASGI app and a real websocket
handshake:

* an unauthenticated caller gets 401, a remote/proxied caller 403 — before any
  code reaches the browser;
* a websocket from a foreign Origin is refused at the handshake;
* an accepted-but-untrusted websocket (a non-browser client, or a page served
  from a public name) receives no eval frame and its ``evalres`` is ignored, so
  it cannot spoof what ``/api/eval`` reports;
* the same-origin UI page on loopback still works, unauthenticated, and is
  trusted — that is what keeps the harness browser and the product UI working.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server import app as app_module
from server.harness import BrowserBridge

TOKEN = "test-harness-token"
TOKEN_HEADER = b"x-megaspeed-token"


# --------------------------------------------------------------------------- #
# ASGI drivers (no httpx in this venv: starlette's TestClient is unavailable)
# --------------------------------------------------------------------------- #
class Reply:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode())


async def _call_http(path, *, method="POST", body=b"", headers=None,
                     client=("127.0.0.1", 4321), host=b"127.0.0.1:8766"):
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", host), *(headers or [])],
        "client": client,
        "server": (host.decode().split(":")[0], 8766),
    }
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    await app_module.app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return Reply(
        start["status"],
        {k.decode().lower(): v.decode() for k, v in start["headers"]},
        payload,
    )


def call(path, **kwargs) -> Reply:
    return asyncio.run(_call_http(path, **kwargs))


class Socket:
    """One ASGI websocket session: send frames in, read frames out."""

    def __init__(self, app, path="/ws", headers=None, client=("127.0.0.1", 5555),
                 host=b"127.0.0.1:8766", query=b""):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.outgoing: asyncio.Queue = asyncio.Queue()
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query,
            "root_path": "",
            "headers": [(b"host", host), *(headers or [])],
            "client": client,
            "server": (host.decode().split(":")[0], 8766),
            "subprotocols": [],
        }
        self.app = app
        self.task = None

    async def open(self) -> "Socket":
        self.task = asyncio.create_task(self.app(self.scope, self._receive, self._send))
        await self.incoming.put({"type": "websocket.connect"})
        first = await asyncio.wait_for(self.outgoing.get(), 1.0)
        if first["type"] == "websocket.accept":
            self.accepted = True
        else:
            self.accepted = False
            self._first = first      # a close, for a refused handshake
            await self.outgoing.put(first)
        return self

    async def _receive(self):
        return await self.incoming.get()

    async def _send(self, message):
        await self.outgoing.put(message)

    async def send_text(self, text: str) -> None:
        await self.incoming.put({"type": "websocket.receive", "text": text})

    async def next_frame(self, timeout: float = 1.0):
        message = await asyncio.wait_for(self.outgoing.get(), timeout)
        if message["type"] == "websocket.send":
            return json.loads(message["text"])
        return message

    async def expect_nothing(self, timeout: float = 0.2) -> None:
        try:
            message = await asyncio.wait_for(self.outgoing.get(), timeout)
        except asyncio.TimeoutError:
            return
        raise AssertionError(f"unexpected frame: {message!r}")

    async def close(self) -> None:
        await self.incoming.put({"type": "websocket.disconnect", "code": 1000})
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.task.cancel()


class BridgeCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            mock.patch.dict(os.environ, {
                app_module.HARNESS_TOKEN_ENV: TOKEN,
                app_module.HARNESS_TOKEN_FILE_ENV: str(self.root / ".harness-token"),
            }),
            mock.patch.object(app_module, "bridge", BrowserBridge()),
            mock.patch.object(app_module, "LOG", False),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()


# --------------------------------------------------------------------------- #
# /api/eval and /api/screenshot
# --------------------------------------------------------------------------- #
class EvalHttpTests(BridgeCase):
    def test_an_unauthenticated_local_caller_is_refused_before_the_browser(self):
        reply = call("/api/eval", body=b'{"code":"1+1"}',
                     headers=[(b"content-type", b"application/json")])
        self.assertEqual(reply.status, 401)
        body = reply.json()
        self.assertFalse(body["ok"])
        self.assertIn("token", body["error"])
        self.assertEqual(app_module.bridge.clients, 0)

    def test_a_wrong_token_is_refused(self):
        reply = call("/api/eval", body=b'{"code":"1+1"}',
                     headers=[(TOKEN_HEADER, b"not-the-token")])
        self.assertEqual(reply.status, 401)

    def test_the_token_opens_the_bridge(self):
        with mock.patch.object(
            app_module.bridge, "eval",
            mock.AsyncMock(return_value={"ok": True, "data": 2, "error": None}),
        ) as ev:
            reply = call("/api/eval", body=b'{"code":"1+1"}',
                         headers=[(TOKEN_HEADER, TOKEN.encode())])
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.json()["data"], 2)
        ev.assert_awaited_once_with("1+1", 10.0)

    def test_a_remote_client_cannot_use_the_bridge_even_with_the_token(self):
        reply = call("/api/eval", body=b'{"code":"1+1"}',
                     headers=[(TOKEN_HEADER, TOKEN.encode())],
                     client=("10.43.1.9", 5000), host=b"127.0.0.1:8766")
        self.assertEqual(reply.status, 403)
        self.assertIn("local-only", reply.json()["error"])

    def test_a_proxied_request_cannot_use_the_bridge(self):
        reply = call("/api/eval", body=b'{"code":"1+1"}',
                     headers=[(TOKEN_HEADER, TOKEN.encode()),
                              (b"x-forwarded-for", b"203.0.113.7")])
        self.assertEqual(reply.status, 403)

    def test_a_public_host_header_cannot_use_the_bridge(self):
        reply = call("/api/eval", body=b'{"code":"1+1"}',
                     headers=[(TOKEN_HEADER, TOKEN.encode())],
                     host=b"megaspeed.internal:8766")
        self.assertEqual(reply.status, 403)

    def test_screenshot_is_gated_the_same_way(self):
        unauthenticated = call("/api/screenshot", body=b'{"selector":".app"}')
        self.assertEqual(unauthenticated.status, 401)
        with mock.patch.object(
            app_module.bridge, "screenshot",
            mock.AsyncMock(return_value={"ok": False, "error": "no browser connected"}),
        ):
            authenticated = call("/api/screenshot", body=b'{"selector":".app"}',
                                 headers=[(TOKEN_HEADER, TOKEN.encode())])
        self.assertEqual(authenticated.status, 200)


class TokenTests(BridgeCase):
    def test_the_generated_token_is_stable_and_private(self):
        path = self.root / "auto-token"
        with mock.patch.dict(os.environ, {
            app_module.HARNESS_TOKEN_ENV: "",
            app_module.HARNESS_TOKEN_FILE_ENV: str(path),
        }), mock.patch.object(app_module, "_auto_token", None):
            first = app_module.harness_token()
            second = app_module.harness_token()
        self.assertEqual(first, second)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(path.read_text().strip(), first)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600, f"token file mode is {oct(mode)}")

    def test_the_env_token_wins_and_no_file_is_written(self):
        path = self.root / "unused-token"
        with mock.patch.dict(os.environ, {app_module.HARNESS_TOKEN_FILE_ENV: str(path)}):
            self.assertEqual(app_module.harness_token(), TOKEN)
        self.assertFalse(path.exists())


# --------------------------------------------------------------------------- #
# /ws: origin allowlist + trusted-only eval frames
# --------------------------------------------------------------------------- #
class WebsocketTests(BridgeCase):
    async def test_a_cross_site_origin_is_refused_at_the_handshake(self):
        socket = await Socket(
            app_module.app,
            headers=[(b"origin", b"https://evil.example")],
        ).open()
        message = await socket.next_frame()
        self.assertEqual(message["type"], "websocket.close")
        self.assertEqual(message["code"], 1008)
        await socket.close()

    async def test_the_same_origin_ui_page_is_trusted(self):
        socket = await Socket(
            app_module.app, headers=[(b"origin", b"http://127.0.0.1:8766")]
        ).open()
        hello = await socket.next_frame()
        self.assertEqual(hello["event"], "hello")
        self.assertTrue(hello["harness"])
        self.assertEqual(app_module.bridge.trusted, 1)
        await socket.close()

    async def test_an_untrusted_client_gets_no_eval_and_cannot_answer_one(self):
        # A non-browser client: no Origin, so it is accepted but never trusted.
        untrusted = await Socket(app_module.app).open()
        hello = await untrusted.next_frame()
        self.assertFalse(hello["harness"])
        self.assertEqual(app_module.bridge.clients, 1)
        self.assertEqual(app_module.bridge.trusted, 0)

        # The server has a websocket attached, but no *trusted* one: eval is not
        # broadcast at all, so there is nothing to spy on or answer.
        result = await app_module.bridge.eval("1+1", timeout=0.2)
        self.assertFalse(result["ok"])
        self.assertIn("no authenticated browser", result["error"])
        await untrusted.expect_nothing()

        # And a forged answer to an eval id it never received is ignored.
        await untrusted.send_text(json.dumps({"evalres": 1, "ok": True, "data": "SPOOFED"}))
        await untrusted.expect_nothing()
        self.assertNotIn(1, app_module.bridge._waiters)
        await untrusted.close()

    async def test_only_the_trusted_browser_can_answer_an_eval(self):
        untrusted = await Socket(app_module.app).open()
        await untrusted.next_frame()          # hello (harness: false)
        trusted = await Socket(
            app_module.app, headers=[(b"origin", b"http://127.0.0.1:8766")]
        ).open()
        await trusted.next_frame()            # hello (harness: true)

        async def drive():
            return await app_module.bridge.eval("document.title", timeout=2.0)

        task = asyncio.create_task(drive())
        frame = await trusted.next_frame()
        self.assertEqual(frame["code"], "document.title")
        # the untrusted client never sees the eval frame ...
        await untrusted.expect_nothing()
        # ... so it cannot pre-empt the real answer even if it guesses the id
        await untrusted.send_text(json.dumps(
            {"evalres": frame["eval"], "ok": True, "data": "SPOOFED-BY-ANY-CLIENT"}
        ))
        await trusted.send_text(json.dumps(
            {"evalres": frame["eval"], "ok": True, "data": "megaspeed"}
        ))
        result = await asyncio.wait_for(task, 2.0)
        self.assertEqual(result["data"], "megaspeed")
        await trusted.close()
        await untrusted.close()

    async def test_a_token_authenticates_a_non_browser_client(self):
        socket = await Socket(
            app_module.app, query=f"token={TOKEN}".encode()
        ).open()
        hello = await socket.next_frame()
        self.assertTrue(hello["harness"])
        await socket.close()

    async def test_a_configured_origin_allowlist_overrides_the_default(self):
        socket = await Socket(
            app_module.app, headers=[(b"origin", b"https://ui.example")]
        ).open()
        refused = await socket.next_frame()
        self.assertEqual(refused["type"], "websocket.close")
        await socket.close()

        with mock.patch.dict(os.environ, {app_module.HARNESS_ORIGINS_ENV: "https://ui.example"}):
            allowed = await Socket(
                app_module.app, headers=[(b"origin", b"https://ui.example")]
            ).open()
            hello = await allowed.next_frame()
            self.assertEqual(hello["event"], "hello")
            # ... but a public origin is still not the local browser: no trust
            self.assertFalse(hello["harness"])
            await allowed.close()


if __name__ == "__main__":
    unittest.main()
