"""megaspeed local dev server.

Read-only over the pseudonymised store: there is no write path, no credential,
and no import of anything under ``ingest/`` except the schema guard's read
helpers. If the UI cannot leak raw text, it cannot leak raw text by accident.

    GET  /api/health                    store status + provenance summary
    GET  /api/events?from=&to=&limit=   pseudonymised event log
    GET  /api/objects?type=             PR / COMMIT / TICKET / PERSON objects
    GET  /api/blocking?level=&days=     wait-hours handover graph (prd/05)
    GET  /api/queues?level=&days=       per-person WIP / throughput series
    GET  /api/delivery?days=            triage list: blocked / oldest waiting
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
import threading
from pathlib import Path

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(os.environ.get("MEGASPEED_APP_DIR", REPO_ROOT / "app")).resolve()
DB_PATH = Path(os.environ.get("MEGASPEED_DB", REPO_ROOT / "data" / "megaspeed.duckdb")).resolve()

sys.path.insert(0, str(REPO_ROOT))
from analysis import ballcourt as analyse  # noqa: E402  (read-only projections)
from ingest import store as pstore  # noqa: E402  (read helpers + schema guard only)
from server.harness import BrowserBridge  # noqa: E402

bridge = BrowserBridge()

LOG = os.environ.get("MEGASPEED_QUIET", "") not in ("1", "true")

# Reconstructing the interval timeline is cheap (one pass over the log) but the
# three analysis views ask for it on every page switch, so keep the last build
# for the current store stamp. Read-only; a store change invalidates it.
_analysis_lock = threading.Lock()
_analysis_cache: tuple[tuple, object] | None = None



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
            return _list_objects(con, type)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


def _list_objects(con, object_type: str | None = None) -> list[dict]:
    """The object table, read directly.

    ``ingest.store.list_objects`` is the canonical reader, but its column list
    tracks whatever ingest is adding this week; this endpoint promises a stable
    shape to the UI, so it reads the stable columns itself and treats newer
    columns as optional.
    """
    columns = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'objects'"
        ).fetchall()
    }
    extra = ", CAST(attributes AS VARCHAR) AS attributes" if "attributes" in columns else ", NULL AS attributes"
    sql = (
        "SELECT object_type, object_id, repo, pr_id, ticket_keys, actor, state,"
        " created_at, closed_at, head_commit, message_class"
        f"{extra} FROM objects"
    )
    params: list = []
    if object_type:
        sql += " WHERE object_type = ?"
        params.append(object_type)
    sql += " ORDER BY object_type, object_id"
    out = []
    for row in con.execute(sql, params).fetchall():
        (otype, oid, repo, pr_id, keys, actor, state, created, closed, head, mclass, attrs) = row
        record = {
            "object_type": otype,
            "object_id": oid,
            "repo": repo,
            "pr_id": pr_id,
            "ticket_keys": list(keys or []),
            "actor": actor,
            "state": state,
            "created_at": pstore.iso(created) if created else None,
            "closed_at": pstore.iso(closed) if closed else None,
            "head_commit": head,
            "message_class": mclass,
        }
        if attrs:
            try:
                record["attributes"] = json.loads(attrs) if isinstance(attrs, str) else attrs
            except json.JSONDecodeError:
                record["attributes"] = {}
        out.append(record)
    return out


# --------------------------------------------------------------------------- #
# analysis views (prd/05): read-only projections of the same event log
# --------------------------------------------------------------------------- #
def _ballcourt(con):
    """Reconstruct the interval timeline once per store version."""
    global _analysis_cache
    stamp = _stamp()
    with _analysis_lock:
        if _analysis_cache is not None and _analysis_cache[0] == stamp:
            return _analysis_cache[1]
    court = analyse.load(con)
    with _analysis_lock:
        _analysis_cache = (stamp, court)
    return court


def _days(days: float | None) -> float | None:
    if days is None:
        return None
    return max(0.1, min(float(days), 3650.0))


def _level(level: str) -> str:
    return level if level in ("queue", "person") else "queue"


@app.get("/api/blocking")
def blocking(level: str = "queue", days: float | None = None):
    """Weighted directed handover graph: ``X -> Y`` = X waited on Y for N hours."""
    if not DB_PATH.exists():
        return _missing_store()
    try:
        with store_conn() as con:
            court = _ballcourt(con)
            window = analyse.window_from_events(court, _days(days))
            return analyse.blocking_graph(court, _level(level), window)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


@app.get("/api/queues")
def queues(level: str = "person", days: float | None = None, bucket_hours: float | None = None):
    """Per-person / per-queue WIP over time, arrivals and completions."""
    if not DB_PATH.exists():
        return _missing_store()
    try:
        with store_conn() as con:
            court = _ballcourt(con)
            window = analyse.window_from_events(court, _days(days))
            return analyse.queue_series(court, _level(level), window, bucket_hours)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


@app.get("/api/delivery")
def delivery(days: float | None = None):
    """Triage list: open PRs, what they wait on, and for how long."""
    if not DB_PATH.exists():
        return _missing_store()
    try:
        with store_conn() as con:
            court = _ballcourt(con)
            window = analyse.window_from_events(court, _days(days))
            return analyse.delivery_list(court, window)
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
