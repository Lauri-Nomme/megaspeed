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

Every query parameter is either honoured or rejected with a 400 that lists the
valid ones — a filter is never silently ignored (BUG-13). Malformed bodies are
400 JSON, never a traceback (BUG-08). A store that an ingest run is writing is
a retryable 503 with no path / PID / username in it (BUG-07).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(os.environ.get("MEGASPEED_APP_DIR", REPO_ROOT / "app")).resolve()
DB_PATH = Path(os.environ.get("MEGASPEED_DB", REPO_ROOT / "data" / "megaspeed.duckdb")).resolve()

# Screenshots may only ever be written *inside* this directory (BUG-08).
SHOT_DIR = Path(
    os.environ.get("MEGASPEED_SHOT_DIR", Path(tempfile.gettempdir()) / "megaspeed-shots")
).expanduser().resolve()

# How long a read waits for an ingest run to release the DuckDB write lock.
STORE_RETRY_ATTEMPTS = max(1, int(os.environ.get("MEGASPEED_STORE_RETRIES", "5")))
STORE_RETRY_DELAY = max(0.0, float(os.environ.get("MEGASPEED_STORE_RETRY_DELAY", "0.12")))

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


class StoreBusy(RuntimeError):
    """The DuckDB file is locked by a writer (an ingest run). Retryable."""


LOCK_HINTS = (
    "could not set lock",
    "conflicting lock",
    "lock on file",
    "being used by another process",
)


def _is_lock_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in LOCK_HINTS)


@contextlib.contextmanager
def store_conn():
    """One short-lived read-only DuckDB connection per request.

    DuckDB allows many readers *or* one writer, so an ingest run makes every
    read fail with "Could not set lock". A run is seconds long for fixtures and
    minutes for a corpus, so the read path retries briefly and then hands the
    caller a :class:`StoreBusy` (a clean, retryable 503) instead of DuckDB's raw
    message — which names the store path, the PID and the OS user.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(str(DB_PATH))
    con = None
    for attempt in range(STORE_RETRY_ATTEMPTS):
        try:
            con = pstore.connect(DB_PATH, read_only=True)
            break
        except Exception as exc:  # noqa: BLE001
            if not _is_lock_error(exc):
                raise
            if attempt + 1 >= STORE_RETRY_ATTEMPTS:
                raise StoreBusy("store is locked by a writer") from None
            time.sleep(STORE_RETRY_DELAY * (attempt + 1))
    try:
        yield con
    finally:
        if con is not None:
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
    if isinstance(exc, StoreBusy):
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": "the store is being written right now (ingest in progress)",
                "retryable": True,
                "retry_after_ms": 600,
                "hint": "retry in a moment; reads resume when the ingest run finishes",
            },
            headers={"Retry-After": "1"},
        )
    if LOG:
        print(f"megaspeed: store read failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return JSONResponse(
        status_code=503,
        content={"ok": False, "error": "store read failed", "reason": type(exc).__name__},
    )


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

# The UI is served by this same server, and the eval/screenshot bridge is driven
# with curl (no Origin header), so nothing legitimate needs a wildcard. Only a
# browser tab on localhost/loopback may make a cross-origin call (BUG-08).
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$",
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["content-type"],
    max_age=600,
)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """A bug is a JSON 500, never a bare 'Internal Server Error' (BUG-08)."""
    if LOG:
        print(f"megaspeed: unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
    return JSONResponse(
        status_code=500, content={"ok": False, "error": "internal error", "reason": type(exc).__name__}
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
MAX_TIMEOUT = 300.0


def _body_timeout(body: dict, default: float) -> float:
    """``timeout`` as a positive, finite number of seconds (BUG-08).

    A numeric string ("10") is accepted because curl users type one; anything
    else is a 400, never a ``ValueError`` traceback.
    """
    raw = body.get("timeout")
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        raise ValueError("timeout must be a number of seconds")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError("timeout must be a number of seconds") from None
    if not math.isfinite(value):
        raise ValueError("timeout must be a finite number of seconds")
    if value <= 0:
        raise ValueError("timeout must be positive")
    return min(value, MAX_TIMEOUT)


def _body_text(body: dict, name: str, *, default: str | None = None,
               allow_number: bool = False) -> str:
    """A non-empty string field, or a clear 400 message."""
    raw = body.get(name)
    if raw is None or raw == "":
        if default is None:
            raise ValueError(f"{name} required")
        return default
    if isinstance(raw, str):
        text = raw.strip()
    elif allow_number and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        text = str(raw)
    else:
        raise ValueError(f"{name} must be a string")
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


@app.post("/api/eval")
async def eval_js(request: Request):
    body = await _json_body(request)
    if body is None:
        return _bad_request("invalid json")
    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object, e.g. {\"code\": \"1+1\"}")
    try:
        code = _body_text(body, "code", allow_number=True)
        timeout = _body_timeout(body, 10.0)
    except ValueError as exc:
        return _bad_request(str(exc), valid=["code", "timeout"])
    return await bridge.eval(code, timeout)


def _shot_path(raw: str) -> Path:
    """Resolve ``out`` inside :data:`SHOT_DIR` or refuse it (BUG-08).

    A bare file name is relative to the screenshot directory; an absolute path
    must land inside it. Symlinks are resolved first, so neither ``..`` nor a
    symlink can escape, and the directory is created lazily by the caller.
    """
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = SHOT_DIR / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(SHOT_DIR)
    except ValueError:
        raise ValueError(
            f"out must stay inside the screenshot directory ({SHOT_DIR}); "
            "pass a file name only"
        ) from None
    if resolved == SHOT_DIR:
        raise ValueError("out must be a file inside the screenshot directory")
    if resolved.suffix.lower() != ".png":
        resolved = resolved.with_suffix(resolved.suffix + ".png" if resolved.suffix else ".png")
        try:
            resolved.relative_to(SHOT_DIR)
        except ValueError:  # pragma: no cover - only reachable via odd suffix tricks
            raise ValueError("out must stay inside the screenshot directory") from None
    return resolved


@app.post("/api/screenshot")
async def screenshot(request: Request):
    body = await _json_body(request)
    if body is None:
        return _bad_request("invalid json")
    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object, e.g. {\"selector\": \".app\"}")
    try:
        selector = _body_text(body, "selector", default=".app")
        timeout = _body_timeout(body, 30.0)
        out = body.get("out")
        if out not in (None, ""):
            if not isinstance(out, str):
                raise ValueError("out must be a file name inside the screenshot directory")
            path = _shot_path(out)
        else:
            path = None
    except ValueError as exc:
        return _bad_request(str(exc), valid=["selector", "out", "timeout"])

    result = await bridge.screenshot(selector, timeout)
    if not result.get("ok"):
        return result
    data = result["data"]
    raw = base64.b64decode(data)
    response = {
        "ok": True,
        "selector": selector,
        "mime": "image/png",
        "bytes": len(raw),
        "data": data,
    }
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        response["out"] = str(path)
    return response


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
