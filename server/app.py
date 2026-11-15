"""megaspeed local dev server.

Read-only over the pseudonymised store: there is no write path, no credential,
and no import of anything under ``ingest/`` except the schema guard's read
helpers. If the UI cannot leak raw text, it cannot leak raw text by accident.

    GET  /api/health                    store status + provenance summary
    GET  /api/events?from=&to=&limit=   pseudonymised event log
    GET  /api/objects?type=             PR / COMMIT / TICKET / PERSON objects
    GET  /ws                            harness websocket (hello/changed/eval)
    POST /api/eval        {code, timeout}
    POST /api/screenshot  {selector, out}
    GET  /                              app/ (vanilla JS + canvas, no build)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(os.environ.get("MEGASPEED_APP_DIR", REPO_ROOT / "app")).resolve()
DB_PATH = Path(os.environ.get("MEGASPEED_DB", REPO_ROOT / "data" / "megaspeed.duckdb")).resolve()

sys.path.insert(0, str(REPO_ROOT))
from ingest import store as pstore  # noqa: E402  (read helpers + schema guard only)
from server.harness import BrowserBridge  # noqa: E402

bridge = BrowserBridge()

LOG = os.environ.get("MEGASPEED_QUIET", "") not in ("1", "true")


@contextlib.contextmanager
def store_conn():
    """One short-lived read-only DuckDB connection per request."""
    if not DB_PATH.exists():
        raise FileNotFoundError(str(DB_PATH))
    con = pstore.connect(DB_PATH, read_only=True)
    try:
        yield con
    finally:
        con.close()


def _missing_store() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "error": f"no store at {DB_PATH}",
            "hint": "run: python3 -m ingest --fixture --n 50",
        },
    )


def _store_error(exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_watch_store())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="megaspeed", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# read API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    if not DB_PATH.exists():
        return _missing_store()
    try:
        with store_conn() as con:
            summary = pstore.summary(con)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    return {
        "ok": True,
        "store": str(DB_PATH),
        "app": "megaspeed",
        "version": app.version,
        "browsers": bridge.clients,
        **summary,
    }


@app.get("/api/events")
def events(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200_000),
):
    if not DB_PATH.exists():
        return _missing_store()
    try:
        start = pstore.to_naive_utc(from_) if from_ else None
        end = pstore.to_naive_utc(to) if to else None
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": f"bad timestamp: {exc}"})
    try:
        with store_conn() as con:
            return pstore.list_events(con, start, end, limit)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


@app.get("/api/objects")
def objects(type: str | None = None):
    if not DB_PATH.exists():
        return _missing_store()
    try:
        with store_conn() as con:
            return pstore.list_objects(con, type)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


# --------------------------------------------------------------------------- #
# harness: eval + screenshot (curl-drivable)
# --------------------------------------------------------------------------- #
@app.post("/api/eval")
async def eval_js(request: Request):
    body = await _json_body(request)
    if body is None:
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid json"})
    code = body.get("code")
    if not code:
        return JSONResponse(status_code=400, content={"ok": False, "error": "code required"})
    timeout = float(body.get("timeout") or 10)
    return await bridge.eval(str(code), timeout)


@app.post("/api/screenshot")
async def screenshot(request: Request):
    body = await _json_body(request)
    if body is None:
        return JSONResponse(status_code=400, content={"ok": False, "error": "invalid json"})
    selector = str(body.get("selector") or ".app")
    out = body.get("out")
    result = await bridge.screenshot(selector, float(body.get("timeout") or 30))
    if not result.get("ok"):
        return result
    raw = base64.b64decode(result["data"])
    if out:
        path = Path(str(out)).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {"ok": True, "selector": selector, "out": str(path), "bytes": len(raw), "mime": "image/png"}
    return {"ok": True, "selector": selector, "data": result["data"], "bytes": len(raw), "mime": "image/png"}


async def _json_body(request: Request):
    try:
        raw = await request.body()
        return json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------- #
# websocket
# --------------------------------------------------------------------------- #
@app.websocket("/ws")
async def websocket(ws: WebSocket):
    await ws.accept()
    await bridge.add(ws)
    try:
        await ws.send_json({"event": "hello", "app": "megaspeed", "clients": bridge.clients})
        while True:
            message = await ws.receive_text()
            await bridge.handle(ws, message)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - a broken client is not a server error
        pass
    finally:
        await bridge.remove(ws)


async def _watch_store() -> None:
    """Poll the store mtime; tell connected browsers that data changed."""
    last = _stamp()
    while True:
        await asyncio.sleep(1.0)
        stamp = _stamp()
        if stamp != last:
            last = stamp
            await bridge.broadcast({"event": "changed", "store": str(DB_PATH)})


def _stamp():
    try:
        st = DB_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


if APP_DIR.exists():
    app.mount("/", StaticFiles(directory=str(APP_DIR), html=True), name="app")
