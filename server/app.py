"""megaspeed local dev server.

Read-only over the pseudonymised store: there is no write path, no credential,
and no import of anything under ``ingest/`` except the schema guard's read
helpers. If the UI cannot leak raw text, it cannot leak raw text by accident.

    GET  /api/health                    store status + provenance summary
    GET  /api/events?from=&to=&repo=&actor=&ticket=&activity=&pr_id=
                     &limit=&offset=&order=          pseudonymised event log
    GET  /api/events/counts?granularity=&…             histogram + window totals
    GET  /api/objects?type=&repo=&pr_id=&actor=&ticket=&limit=&offset=
                                                        PR / COMMIT / TICKET / … objects
    GET  /api/blocking?level=&days=     wait-hours handover graph (prd/05)
    GET  /api/queues?level=&days=       per-person WIP / throughput series
    GET  /api/delivery?days=            triage list: blocked / oldest waiting
    GET  /ws                            harness websocket (hello/changed/eval)
    POST /api/eval        {code, timeout}
    POST /api/screenshot  {selector, out, timeout}
    GET  /                              app/ (vanilla JS + canvas, no build)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import sys
import threading
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(os.environ.get("MEGASPEED_APP_DIR", REPO_ROOT / "app")).resolve()
DB_PATH = Path(os.environ.get("MEGASPEED_DB", REPO_ROOT / "data" / "megaspeed.duckdb")).resolve()

sys.path.insert(0, str(REPO_ROOT))
from analysis import ballcourt as analyse  # noqa: E402  (read-only projections)
from ingest import store as pstore  # noqa: E402  (read helpers + schema guard only)
from ingest import taxonomy  # noqa: E402  (closed vocabularies, read-only)
from server import query as query  # noqa: E402  (filters, paging, aggregation)
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


def _bad_request(message: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=400, content={"ok": False, "error": message, **extra})


def _unknown_params(request: Request, valid: tuple[str, ...]) -> JSONResponse | None:
    """Reject query parameters the endpoint does not implement (BUG-13)."""
    unknown = sorted(set(request.query_params) - set(valid))
    if not unknown:
        return None
    return _bad_request(
        f"unknown query parameter(s): {', '.join(unknown)}",
        valid=sorted(valid),
        hint="every parameter is either honoured or rejected; nothing is silently ignored",
    )


def _int_param(request: Request, name: str, default=None, minimum: int = 0,
               maximum: int | None = None) -> int | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _num_param(request: Request, name: str, default=None, minimum: float | None = None,
               maximum: float | None = None) -> float | None:
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


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
def health(request: Request):
    unknown = _unknown_params(request, ())
    if unknown is not None:
        return unknown
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
def events(request: Request):
    """A page of the event log.

    ``limit``/``offset`` page it (GAP-04); ``X-Total-Count`` says how many rows
    match so a client never has to download them to count them. Filters are
    real: ``repo``, ``actor``, ``activity``, ``ticket`` and ``pr_id`` are
    applied in SQL, and anything not listed is a 400 (BUG-13).
    """
    unknown = _unknown_params(request, query.EVENT_PARAMS)
    if unknown is not None:
        return unknown
    try:
        start, end = query.parse_range(
            request.query_params.get("from"), request.query_params.get("to")
        )
        limit = _int_param(request, "limit", default=None, minimum=1, maximum=200_000)
        offset = _int_param(request, "offset", default=0, minimum=0, maximum=1_000_000_000)
        pr_id = _int_param(request, "pr_id", default=None, minimum=0)
    except ValueError as exc:
        return _bad_request(str(exc), valid=sorted(query.EVENT_PARAMS))
    activity = request.query_params.get("activity") or None
    if activity is not None and activity not in taxonomy.ACTIVITIES:
        return _bad_request(
            f"activity {activity!r} is outside the closed vocabulary",
            valid=sorted(taxonomy.ACTIVITIES),
        )
    order = (request.query_params.get("order") or "asc").lower()
    if order not in ("asc", "desc"):
        return _bad_request("order must be 'asc' or 'desc'", valid=["asc", "desc"])
    if not DB_PATH.exists():
        return _missing_store()
    try:
        with store_conn() as con:
            rows, total = query.list_events(
                con, start, end, limit, offset or 0, order,
                repo=request.query_params.get("repo") or None,
                actor=request.query_params.get("actor") or None,
                activity=activity,
                pr_id=pr_id,
                ticket=request.query_params.get("ticket") or None,
            )
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    return JSONResponse(
        rows,
        headers={
            "X-Total-Count": str(total),
            "X-Limit": str(limit or total),
            "X-Offset": str(offset or 0),
        },
    )


@app.get("/api/events/counts")
def event_counts(request: Request):
    """Per-bucket event counts plus window totals — no per-event rows (GAP-04)."""
    unknown = _unknown_params(request, query.COUNT_PARAMS)
    if unknown is not None:
        return unknown
    try:
        start, end = query.parse_range(
            request.query_params.get("from"), request.query_params.get("to")
        )
        pr_id = _int_param(request, "pr_id", default=None, minimum=0)
    except ValueError as exc:
        return _bad_request(str(exc), valid=sorted(query.COUNT_PARAMS))
    activity = request.query_params.get("activity") or None
    if activity is not None and activity not in taxonomy.ACTIVITIES:
        return _bad_request(
            f"activity {activity!r} is outside the closed vocabulary",
            valid=sorted(taxonomy.ACTIVITIES),
        )
    if not DB_PATH.exists():
        return _missing_store()
    try:
        with store_conn() as con:
            return query.event_counts(
                con, start, end,
                request.query_params.get("granularity") or "day",
                repo=request.query_params.get("repo") or None,
                actor=request.query_params.get("actor") or None,
                activity=activity,
                pr_id=pr_id,
                ticket=request.query_params.get("ticket") or None,
            )
    except ValueError as exc:
        return _bad_request(str(exc), valid=sorted(query.GRANULARITIES))
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


@app.get("/api/objects")
def objects(request: Request):
    """A page of the object table; filterable by type/repo/pr_id/actor/ticket."""
    unknown = _unknown_params(request, query.OBJECT_PARAMS)
    if unknown is not None:
        return unknown
    object_type = request.query_params.get("type") or None
    if object_type is not None and object_type not in taxonomy.OBJECT_TYPES:
        return _bad_request(
            f"type {object_type!r} is outside the closed vocabulary",
            valid=sorted(taxonomy.OBJECT_TYPES),
        )
    try:
        pr_id = _int_param(request, "pr_id", default=None, minimum=0)
        limit = _int_param(request, "limit", default=None, minimum=1, maximum=200_000)
        offset = _int_param(request, "offset", default=0, minimum=0, maximum=1_000_000_000)
    except ValueError as exc:
        return _bad_request(str(exc), valid=sorted(query.OBJECT_PARAMS))
    if not DB_PATH.exists():
        return _missing_store()
    try:
        with store_conn() as con:
            rows, total = query.list_objects(
                con,
                object_type=object_type,
                repo=request.query_params.get("repo") or None,
                pr_id=pr_id,
                actor=request.query_params.get("actor") or None,
                ticket=request.query_params.get("ticket") or None,
                limit=limit,
                offset=offset or 0,
            )
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    return JSONResponse(
        rows,
        headers={"X-Total-Count": str(total), "X-Limit": str(limit or total), "X-Offset": str(offset or 0)},
    )


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
    if level not in ("queue", "person"):
        raise ValueError("level must be 'queue' or 'person'")
    return level


ANALYSIS_PARAMS = {
    "blocking": ("level", "days"),
    "queues": ("level", "days", "bucket_hours"),
    "delivery": ("days",),
}


@app.get("/api/blocking")
def blocking(request: Request):
    """Weighted directed handover graph: ``X -> Y`` = X waited on Y for N hours."""
    unknown = _unknown_params(request, ANALYSIS_PARAMS["blocking"])
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
        level = _level(request.query_params.get("level") or "queue")
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(ANALYSIS_PARAMS["blocking"]))
    if not DB_PATH.exists():
        return _missing_store()
    try:
        with store_conn() as con:
            court = _ballcourt(con)
            window = analyse.window_from_events(court, _days(days))
            return analyse.blocking_graph(court, level, window)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


@app.get("/api/queues")
def queues(request: Request):
    """Per-person / per-queue WIP over time, arrivals and completions."""
    unknown = _unknown_params(request, ANALYSIS_PARAMS["queues"])
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
        bucket_hours = _num_param(request, "bucket_hours", minimum=1, maximum=720)
        level = _level(request.query_params.get("level") or "person")
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(ANALYSIS_PARAMS["queues"]))
    if not DB_PATH.exists():
        return _missing_store()
    try:
        with store_conn() as con:
            court = _ballcourt(con)
            window = analyse.window_from_events(court, _days(days))
            return analyse.queue_series(court, level, window, bucket_hours)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


@app.get("/api/delivery")
def delivery(request: Request):
    """Triage list: open PRs, what they wait on, and for how long."""
    unknown = _unknown_params(request, ANALYSIS_PARAMS["delivery"])
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(ANALYSIS_PARAMS["delivery"]))
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
