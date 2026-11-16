"""Eval / screenshot bridge between the API and any attached browser.

Same protocol as the mosaic harness, because muscle memory is a feature:

    server -> browser   {"event": "hello"}            on connect
    server -> browser   {"event": "changed"}          when the store moves
    server -> browser   {"eval": <id>, "code": "..."}  run this JS
    browser -> server   {"evalres": <id>, "ok": true, "data": ...}
    browser -> server   {"evalchunk": {"id":..,"i":..,"n":..,"data":".."}}
                        for results too large for one frame (~1 MB PNGs)
    browser -> server   {"ping": <ts>}  ->  {"pong": <ts>}

There is no browser dependency baked in: if nothing is attached, ``eval``
returns ``{"ok": false, "error": "no browser connected"}``, which is the
correct answer in CI and the wrong answer only if you expected a screenshot.

**Trust** (SEC-2): a client is either *trusted* (authenticated: it presented the
harness token, or it is the same-origin UI page on loopback) or a *notification
client*. ``eval`` frames are only delivered to trusted clients, and only trusted
clients may answer them — so a cross-site page or any other websocket client can
watch ``hello``/``changed`` but cannot receive an eval or spoof its result.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

CHUNK_THRESHOLD = 200_000  # characters of JSON per frame


class BrowserBridge:
    def __init__(self) -> None:
        self._clients: set[Any] = set()
        self._trusted: set[Any] = set()
        self._lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._seq = 0
        self._waiters: dict[int, asyncio.Future] = {}
        self._chunks: dict[int, dict] = {}

    # -- connection registry ---------------------------------------------
    async def add(self, ws, authenticated: bool = False) -> None:
        async with self._lock:
            self._clients.add(ws)
            if authenticated:
                self._trusted.add(ws)

    async def remove(self, ws) -> None:
        async with self._lock:
            self._clients.discard(ws)
            self._trusted.discard(ws)

    def is_trusted(self, ws) -> bool:
        return ws in self._trusted

    @property
    def clients(self) -> int:
        return len(self._clients)

    @property
    def trusted(self) -> int:
        """Clients allowed to receive (and answer) eval frames."""
        return len(self._trusted)

    async def broadcast(self, payload: dict, trusted_only: bool = False) -> None:
        async with self._lock:
            clients = list(self._trusted if trusted_only else self._clients)
        if not clients:
            return
        async with self._send_lock:
            for ws in clients:
                try:
                    await ws.send_json(payload)
                except Exception:  # noqa: BLE001 - drop dead clients silently
                    await self.remove(ws)

    # -- client -> server -------------------------------------------------
    async def handle(self, ws, message: str) -> None:
        try:
            msg = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        if "evalchunk" in msg:
            # Only the browser the eval was sent to may answer it (SEC-2).
            if not self.is_trusted(ws):
                return
            chunk = msg.get("evalchunk") or {}
            self._accept_chunk(chunk)
            return

        if "evalres" in msg:
            if not self.is_trusted(ws):
                return
            eid = int(msg.get("evalres") or 0)
            future = self._waiters.get(eid)
            if future and not future.done():
                future.set_result(
                    {"ok": bool(msg.get("ok")), "data": msg.get("data"), "error": msg.get("error")}
                )
            return

        if "ping" in msg:
            try:
                await ws.send_json({"pong": msg.get("ping")})
            except Exception:  # noqa: BLE001
                pass

    def _accept_chunk(self, chunk: dict) -> None:
        try:
            eid = int(chunk["id"])
            index = int(chunk["i"])
            count = int(chunk["n"])
        except (KeyError, TypeError, ValueError):
            return
        buffer = self._chunks.setdefault(eid, {"n": count, "parts": {}})
        buffer["parts"][index] = chunk.get("data") or ""
        if len(buffer["parts"]) < buffer["n"]:
            return
        joined = "".join(buffer["parts"].get(i, "") for i in range(buffer["n"]))
        self._chunks.pop(eid, None)
        try:
            payload = json.loads(joined)
        except json.JSONDecodeError:
            payload = {"ok": False, "error": "chunk decode failed"}
        future = self._waiters.get(eid)
        if future and not future.done():
            future.set_result(
                {
                    "ok": bool(payload.get("ok")),
                    "data": payload.get("data"),
                    "error": payload.get("error"),
                }
            )

    # -- server -> browser ------------------------------------------------
    async def eval(self, code: str, timeout: float = 10.0) -> dict:
        async with self._lock:
            trusted = len(self._trusted)
            clients = len(self._clients)
        if not trusted:
            if clients:
                return {
                    "ok": False,
                    "error": "no authenticated browser connected (the eval bridge only "
                             "answers the UI on loopback or a client with the harness token)",
                }
            return {"ok": False, "error": "no browser connected"}
        async with self._lock:
            self._seq += 1
            eid = self._seq
            future = asyncio.get_running_loop().create_future()
            self._waiters[eid] = future
        try:
            await self.broadcast({"eval": eid, "code": code}, trusted_only=True)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "timeout"}
        finally:
            self._waiters.pop(eid, None)
            self._chunks.pop(eid, None)

    async def screenshot(self, selector: str = ".app", timeout: float = 30.0) -> dict:
        """Capture an element in the browser. Returns a base64 PNG in ``data``."""
        code = f"window.__megaspeedShot({json.dumps(selector)})"
        result = await self.eval(code, timeout)
        if not result.get("ok"):
            return result
        data = result.get("data")
        if isinstance(data, dict):  # tolerate a richer return shape
            data = data.get("data")
        if not isinstance(data, str) or not data:
            return {"ok": False, "error": "browser returned no image data"}
        if not data.startswith("data:image/png;base64,"):
            return {"ok": False, "error": "browser returned an unexpected data URL"}
        return {
            "ok": True,
            "selector": selector,
            "mime": "image/png",
            "data": data.split(",", 1)[1],
        }
