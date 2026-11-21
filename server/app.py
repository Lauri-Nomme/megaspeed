"""megaspeed local dev server.

Read-only over the pseudonymised store, with one deliberate exception: the
team's own **annotations** (prd/61) are first-party metadata the team types in,
so the server may write — and only write — an ``annotations`` row. There is no
write path into the event log, no credential, and no import of anything under
``ingest/`` except the schema guard's read helpers. If the UI cannot leak raw
text, it cannot leak raw text by accident.

**Tenants** (prd/12): one dataset per ``<tenants-dir>/<tenant>.duckdb``. The
list is the directory itself (no registry file to drift), and every read
endpoint takes an optional ``?tenant=<name>`` that selects the store; without
it the first tenant (or ``MEGASPEED_TENANT``/``MEGASPEED_DB``) is used. An
unknown name is a clean 404 that lists the real ones — never a fallback to
another dataset, because silently answering about the wrong environment is the
one failure this whole feature exists to prevent.

    GET  /api/meta                      build identity: version / tag / sha (prd/32)
    GET  /api/tenants                   datasets + per-tenant health
    GET  /api/health?tenant=            store status + provenance summary
    GET  /api/events?from=&to=&repo=&actor=&ticket=&activity=&pr_id=
                     &limit=&offset=&order=&tenant=   pseudonymised event log
    GET  /api/events/counts?granularity=&…&tenant=      histogram + window totals
    GET  /api/objects?type=&repo=&pr_id=&actor=&ticket=&limit=&offset=&tenant=
                                                        PR / COMMIT / TICKET / … objects
    GET  /api/ticket/{key}?tenant=            one ticket's full cycle (prd/25):
                                              story + segments + PRs + sprints
                                              + worklogs + events + prose answer
    GET  /api/blocking?level=&days=&repo=&tenant=   wait-hours handover graph (prd/05)
    GET  /api/queues?level=&days=&repo=&tenant=     per-person WIP / throughput series
    GET  /api/delivery?days=&repo=&tenant=          triage list: blocked / oldest waiting
    GET  /api/aging?tenant=&days=&repo=&kind=&stage=&only=&limit=
                                        in-flight age vs the team's own p50/p85/p95
                                        and the ranked chase list (prd/59)
    GET  /api/insights?level=&days=&repo=&tenant=   ranked "change this" rules (prd/17)
    GET  /api/verdict?tenant=&days=&repo=           the one constraint, its holders,
                                        and the exploit/elevate ladder (prd/58)
    GET  /api/pr-size?tenant=&size_commits=&size_lines=
                                        PR-size distributions + caps (prd/39)
    GET  /api/sprints?days=&tenant=     sprint lens (prd/26 P6): bands, scope
                                        (in/out of a sprint) and carry-over
    GET  /api/trend?tenant=&metric=…&bucket=&days=&repo=
                                        one headline metric as a time series,
                                        bucketed by week or sprint (prd/50),
                                        with annotations, spans and stability (prd/61)
    GET  /api/trend/compare?tenant=&metric=&split=&bucket=&days=&repo=
                                        item-level before/after around one date,
                                        with a bootstrap CI and an outcome (prd/61)
    GET  /api/annotations?tenant=&repo=  the team's dated policy changes (prd/61)
    POST /api/annotations                record one (kind is a closed vocabulary;
                                        the optional note is first-party metadata)
    DELETE /api/annotations/{id}         remove one
    GET  /api/simulate?days=&scenario=&repo=&tenant= policy lab: calibrated model,
                                              validation gate + one scenario (prd/23)
    GET  /api/calendar?tenant=          effective business-hours calendar (prd/19)
    GET  /ws                            harness websocket (hello/changed/eval)
    POST /api/eval        {code, timeout}          local-only + harness token
    POST /api/screenshot  {selector, out, timeout} local-only + harness token
    GET  /                              app/ (vanilla JS + canvas, no build)

Every query parameter is either honoured or rejected with a 400 that lists the
valid ones — a filter is never silently ignored (BUG-13). Malformed bodies are
400 JSON, never a traceback (BUG-08). A store that an ingest run is writing is
a retryable 503 with no path / PID / username in it (BUG-07).

The paged endpoints echo the dataset they answered from in ``X-Tenant`` (next to
``X-Total-Count``), so a client can prove which store a row came from.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import os
import re
import secrets
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(os.environ.get("MEGASPEED_APP_DIR", REPO_ROOT / "app")).resolve()

# The store MEGASPEED_DB names is the *default tenant*: its directory is the
# tenant directory and its stem is the preferred tenant name. With no env at
# all, the default is the first ``data/*.duckdb`` by name.
_ENV_DB = os.environ.get("MEGASPEED_DB")
DB_PATH = Path(_ENV_DB or (REPO_ROOT / "data" / "megaspeed.duckdb")).resolve()
TENANTS_DIR = Path(os.environ.get("MEGASPEED_TENANTS_DIR", DB_PATH.parent)).resolve()
DEFAULT_TENANT = (
    os.environ.get("MEGASPEED_TENANT") or (DB_PATH.stem if _ENV_DB else "")
).strip()

# The one extra query parameter every read endpoint accepts (prd/12).
TENANT_PARAM = "tenant"

# The committed default business-hours calendar (prd/19). A tenant extends it in
# ``<tenants-dir>/<tenant>/calendar.yaml``. No default file means wall-clock
# hours, which is what every endpoint used before the calendar existed.
CALENDAR_CONFIG = Path(
    os.environ.get("MEGASPEED_CALENDAR_CONFIG", REPO_ROOT / "config" / "calendar.default.yaml")
).expanduser()

# Screenshots may only ever be written *inside* this directory (BUG-08).
SHOT_DIR = Path(
    os.environ.get("MEGASPEED_SHOT_DIR", Path(tempfile.gettempdir()) / "megaspeed-shots")
).expanduser().resolve()

# --------------------------------------------------------------------------- #
# harness bridge security (SEC-2)
# --------------------------------------------------------------------------- #
# ``/api/eval`` runs arbitrary JS in whatever browser is attached and
# ``/api/screenshot`` writes a file, so the bridge is *not* part of the read-only
# product surface. Three independent gates:
#
#   1. transport: only a *direct loopback* request may drive it — loopback peer,
#      loopback ``Host``, no proxy/forwarding header. A request that arrived
#      through nginx is refused even when it carries a valid token.
#   2. a shared secret: ``MEGASPEED_HARNESS_TOKEN``, or a token published to
#      ``MEGASPEED_HARNESS_TOKEN_FILE`` (default ``<tenants-dir>/.harness-token``,
#      mode 0600) so a local ``curl`` can read it. Give each server its own
#      token file to run several at once; a file that already holds a token is
#      adopted, not overwritten, so a throwaway server cannot clobber the live
#      one's. Without the secret, a cross-site page in the operator's browser
#      could still POST to ``127.0.0.1`` (CORS blocks reading the answer, not
#      running the request).
#   3. the websocket only delivers eval frames to, and only accepts answers
#      from, *trusted* clients: the token, or the same-origin UI page on
#      loopback. Everyone else is a notification-only client (hello/changed),
#      so a cross-site or remote websocket can neither receive an eval nor spoof
#      its result.
#
# The UI keeps working unchanged: it opens ``/ws`` from the same origin, which
# is trusted when the page itself was served over loopback (the harness
# browser's case) and notification-only otherwise.
HARNESS_TOKEN_ENV = "MEGASPEED_HARNESS_TOKEN"
HARNESS_TOKEN_FILE_ENV = "MEGASPEED_HARNESS_TOKEN_FILE"
HARNESS_ORIGINS_ENV = "MEGASPEED_WS_ORIGINS"
TOKEN_HEADER = "x-megaspeed-token"
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
PROXY_HEADERS = ("x-forwarded-for", "x-real-ip", "forwarded", "x-forwarded-host")

_auto_token: str | None = None
_auto_token_lock = threading.Lock()

# How long a read waits for an ingest run to release the DuckDB write lock.
STORE_RETRY_ATTEMPTS = max(1, int(os.environ.get("MEGASPEED_STORE_RETRIES", "5")))
STORE_RETRY_DELAY = max(0.0, float(os.environ.get("MEGASPEED_STORE_RETRY_DELAY", "0.12")))

sys.path.insert(0, str(REPO_ROOT))
from analysis import ballcourt as analyse  # noqa: E402  (read-only projections)
from analysis import advice as advice_crosscheck  # noqa: E402  (prd/43 §1)
from analysis import aging as aging_view  # noqa: E402  (prd/59 aging WIP)
from analysis import annotations as annotation_view  # noqa: E402  (prd/61)
from analysis import before_after as before_after_view  # noqa: E402  (prd/61)
from analysis import calendar as workcal  # noqa: E402  (prd/19 business hours)
from analysis import jira as jira_analysis  # noqa: E402  (prd/21 sprints)
from analysis import rules as insights_rules  # noqa: E402  (prd/17 suggestions)
from analysis import simulation as policy_lab  # noqa: E402  (prd/23 policy lab)
from analysis import tickets as ticket_view  # noqa: E402  (prd/25 ticket detail)
from analysis import trend as trend_view  # noqa: E402  (prd/50 time series)
from analysis import verdict as verdict_view  # noqa: E402  (prd/58 the verdict)
from ingest import store as pstore  # noqa: E402  (read helpers + schema guard only)
from ingest import taxonomy  # noqa: E402  (closed vocabularies, read-only)
from server import query as query  # noqa: E402  (filters, paging, aggregation)
from server import tenants as tenantreg  # noqa: E402  (tenant = data/<name>.duckdb)
from server import version as build  # noqa: E402  (prd/32: tag + short SHA)
from server.harness import BrowserBridge  # noqa: E402

bridge = BrowserBridge()

LOG = os.environ.get("MEGASPEED_QUIET", "") not in ("1", "true")

# Reconstructing the interval timeline is cheap (one pass over the log) but the
# three analysis views ask for it on every page switch, so keep the last build
# for the current (tenant, store version). The same entry also carries the Jira
# layer, threads, bots and PR sizes (``_AnalysisBundle``) so a new ticket key or
# a second endpoint never rebuilds them. Read-only; a store change or a tenant
# switch invalidates it.
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
def store_conn(path: Path | None = None):
    """One short-lived read-only DuckDB connection per request.

    DuckDB allows many readers *or* one writer, so an ingest run makes every
    read fail with "Could not set lock". A run is seconds long for fixtures and
    minutes for a corpus, so the read path retries briefly and then hands the
    caller a :class:`StoreBusy` (a clean, retryable 503) instead of DuckDB's raw
    message — which names the store path, the PID and the OS user.
    """
    target = Path(path) if path is not None else DB_PATH
    if not target.exists():
        raise FileNotFoundError(target.name)
    con = None
    for attempt in range(STORE_RETRY_ATTEMPTS):
        try:
            con = pstore.connect(target, read_only=True)
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
    # The directory name, not the absolute path: an error body is not the place
    # to publish the server's home directory (BUG-07 / BUG-08).
    return JSONResponse(
        status_code=503,
        content={
            "ok": False,
            "error": f"no tenant stores in {TENANTS_DIR.name}/",
            "hint": "run: python3 -m ingest --fixture --n 50 (writes data/megaspeed.duckdb), "
                    "or drop a store in as data/<tenant>.duckdb",
        },
    )


def _store_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, FileNotFoundError):
        return _missing_store()
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


app = FastAPI(title="megaspeed", version=build.VERSION_INFO["version"], lifespan=lifespan)

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

# Responses are JSON (insights is 22.4 MB at 5,240 PRs, a full event page
# 35.2 MB), so gzip is the cheapest win in the product: 22.4 MB → 1.1 MB (5%)
# and 35.2 MB → 2.0 MB, with no API change. Level 6 is the measured setting
# (level 9 buys ~1% for several times the CPU on this payload). ``minimum_size``
# keeps the small replies (health, calendar, a 1 KB ticket row) uncompressed,
# and Starlette's middleware leaves non-HTTP scopes (``/ws``) untouched.
# Added after CORS so it wraps it and compresses the error/preflight bodies too.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """A bug is a JSON 500, never a bare 'Internal Server Error' (BUG-08)."""
    if LOG:
        print(f"megaspeed: unhandled {type(exc).__name__}: {exc}", file=sys.stderr)
    return JSONResponse(
        status_code=500, content={"ok": False, "error": "internal error", "reason": type(exc).__name__}
    )


# --------------------------------------------------------------------------- #
# tenants (prd/12): which store answers this request
# --------------------------------------------------------------------------- #
class UnknownTenant(LookupError):
    """The request named a tenant that is not a store in the tenant directory."""


def _tenant_paths() -> dict[str, Path]:
    """Every tenant, derived from the directory (no registry file to drift)."""
    return tenantreg.discover(TENANTS_DIR)


def _tenant_default(paths: dict[str, Path]) -> str | None:
    return tenantreg.default_name(paths, DEFAULT_TENANT)


def _tenant_of(request: Request) -> tuple[str, Path]:
    """The (name, store) this request reads.

    ``?tenant=`` names one explicitly; otherwise the default tenant answers. A
    name that is not a store in the directory raises :class:`UnknownTenant` —
    the caller turns that into a 404 rather than quietly reading another
    dataset, because answering about the wrong environment is exactly the
    mistake tenants exist to prevent.
    """
    paths = _tenant_paths()
    raw = request.query_params.get(TENANT_PARAM)
    if raw is None or raw == "":
        name = _tenant_default(paths)
        if name is None:
            raise UnknownTenant("")
        return name, paths[name]
    if raw not in paths:
        raise UnknownTenant(raw)
    return raw, paths[raw]


def _tenant_error(name: str) -> JSONResponse:
    """404 for an unknown tenant; 503 when there are no tenants at all."""
    if not name:
        return _missing_store()
    return JSONResponse(
        status_code=404,
        content={
            "ok": False,
            "error": f"unknown tenant {name!r}",
            "valid": sorted(_tenant_paths()),
            "hint": "a tenant is one store: data/<tenant>.duckdb",
        },
    )


def _params(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """An endpoint's own parameters plus the tenant selector."""
    out: list[str] = []
    for group in groups:
        out.extend(group)
    return (*out, TENANT_PARAM)


def _file_stamp(path: Path):
    """Cheap identity for a store file: any rewrite (or touch) changes it."""
    try:
        st = Path(path).stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


# prd/61: the one write path is an annotation. DuckDB's file mtime can be coarse
# (a small row may land inside the same ns tick), so a successful write also
# bumps this in-process stamp. The rows live in the store, so there is no
# separate annotations cache or mtime key: every trend/compare cache key that
# includes the store stamp invalidates naturally, exactly like an ingest run.
_store_stamp_bump: dict[str, int] = {}
_store_stamp_lock = threading.Lock()
# DuckDB allows one writer; serialize the annotation writes so two clicks
# cannot race each other into a lock error.
_annotation_write_lock = threading.Lock()


def _store_stamp(path: Path):
    with _store_stamp_lock:
        bump = _store_stamp_bump.get(str(path), 0)
    return (_file_stamp(path), bump)


def _bump_store_stamp(path: Path) -> None:
    with _store_stamp_lock:
        key = str(path)
        _store_stamp_bump[key] = _store_stamp_bump.get(key, 0) + 1


class StoreReadOnly(RuntimeError):
    """The tenant store (or its directory) is not writable — a mirror install."""


@contextlib.contextmanager
def store_write_conn(path: Path):
    """One short-lived **read-write** connection for a first-party write.

    The only writer is the annotations endpoint (prd/61). A mirror install (a
    read-only file, or a read-only directory for DuckDB's WAL) gets a clean
    :class:`StoreReadOnly` — it still *reads* annotations. The caller owns the
    transaction, so a failure leaves no partial row.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(target.name)
    if not os.access(target, os.W_OK) or not os.access(target.parent, os.W_OK):
        raise StoreReadOnly("the store is not writable (mirror install)")
    con = None
    for attempt in range(STORE_RETRY_ATTEMPTS):
        try:
            con = pstore.connect(target)
            break
        except Exception as exc:  # noqa: BLE001
            if not _is_lock_error(exc):
                raise
            if attempt + 1 >= STORE_RETRY_ATTEMPTS:
                raise StoreBusy("store is locked by a writer") from None
            time.sleep(STORE_RETRY_DELAY * (attempt + 1))
    try:
        # A store created before prd/61 has no annotations table; the additive
        # migration is the schema file itself.
        pstore.ensure_schema(con)
        yield con
    finally:
        if con is not None:
            con.close()


# Per-tenant health is recomputed only when the file changes; a busy store is
# never cached, so its row recovers as soon as the ingest run finishes.
_health_lock = threading.Lock()
_health_cache: dict[str, tuple] = {}


def _tenant_health(path: Path) -> dict:
    stamp = _file_stamp(path)
    with _health_lock:
        hit = _health_cache.get(str(path))
    if hit is not None and hit[0] == stamp:
        return hit[1]
    try:
        with store_conn(path) as con:
            summary = pstore.summary(con)
        payload = {
            "ok": True,
            "events": summary["events"],
            "prs": summary["prs"],
            "commits": summary["commits"],
            "source": summary["source"],
            "as_of": summary["as_of"],
            "first_ts": summary["first_ts"],
            "last_ts": summary["last_ts"],
            # prd/63 F27: a combined tenant's per-source runs, so Sources can
            # break the dataset down instead of naming only the latest source.
            "runs": summary.get("runs") or [],
        }
    except StoreBusy:
        return {"ok": False, "error": "the store is being written right now", "retryable": True}
    except FileNotFoundError:
        return {"ok": False, "error": "the store file is gone"}
    except Exception as exc:  # noqa: BLE001 - one bad store must not hide the rest
        if LOG:
            print(f"megaspeed: tenant probe failed: {type(exc).__name__}", file=sys.stderr)
        return {"ok": False, "error": "store read failed", "reason": type(exc).__name__}
    with _health_lock:
        _health_cache[str(path)] = (stamp, payload)
    return payload


# --------------------------------------------------------------------------- #
# read API
# --------------------------------------------------------------------------- #
@app.get("/api/tenants")
def tenants(request: Request):
    """Every dataset this server can read, with its health (prd/12).

    ``default`` is the tenant that answers a request without ``?tenant=`` — the
    UI uses it when nothing is remembered. Each entry carries enough to label a
    switcher row honestly (PRs, events, source) without opening a second
    connection per page.
    """
    unknown = _unknown_params(request, ())
    if unknown is not None:
        return unknown
    paths = _tenant_paths()
    default = _tenant_default(paths)
    rows = []
    for name, path in paths.items():
        entry = {"name": name, "default": name == default, "store": str(path)}
        entry.update(_tenant_health(path))
        rows.append(entry)
    return {"ok": True, "default": default, "count": len(rows), "tenants": rows}


@app.get("/api/meta")
def meta(request: Request):
    """The build identity (prd/32): which tag + commit produced this server.

    ``version`` is the full ``git describe`` answer (``v0.9.0-3-g88f72bb``, or
    just the SHA before the first release tag), ``tag``/``sha`` are that string
    split for a UI that wants to render ``v0.9.0 · 88f72bb``, and
    ``build_time`` is when the build was made (HEAD commit / VERSION file),
    not when the process started. A store-less server still answers: the
    version is a code identity, not tenant data.
    """
    # prd/63 F14: every other endpoint takes `?tenant=`; refusing it here meant
    # the build identity could not be asked for beside a named dataset. The
    # answer is tenant-independent, but the named tenant is validated (a typo
    # is a 404, never a silent default) and echoed for the caller.
    unknown = _unknown_params(request, _params(()))
    if unknown is not None:
        return unknown
    name = None
    raw = request.query_params.get(TENANT_PARAM)
    if raw:
        try:
            name, _path = _tenant_of(request)
        except UnknownTenant as exc:
            return _tenant_error(exc.args[0])
    return {"ok": True, **build.VERSION_INFO, "tenant": name}


@app.get("/api/health")
def health(request: Request):
    unknown = _unknown_params(request, _params(()))
    if unknown is not None:
        return unknown
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            summary = pstore.summary(con)
            # concepts.md §2: the UI must not offer a bot as a person scope.
            # One small PERSON read, not a full analysis build.
            bots = insights_rules.load_bots(con)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    return {
        "ok": True,
        "tenant": tenant,
        "store": str(path),
        "app": "megaspeed",
        "version": app.version,
        "browsers": bridge.clients,
        # of which this many may receive/answer eval frames (SEC-2)
        "harness_browsers": bridge.trusted,
        "calendar": _calendar_brief(tenant),
        "bots": sorted(bots),
        **summary,
    }


@app.get("/api/calendar")
def calendar(request: Request):
    """The effective business-hours calendar for a tenant (prd/19).

    Shows exactly what the analysis is using — timezone, working window,
    per-person overrides, resolved holidays, vacations — and where it came from,
    so a surprising number can be traced to a config line instead of guessed at.
    ``hours_mode`` is ``business`` when a calendar applies, ``wall-clock`` when
    the default file is missing or broken.
    """
    unknown = _unknown_params(request, _params(()))
    if unknown is not None:
        return unknown
    try:
        tenant, _path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    calendar_obj, error = _calendar_for(tenant)
    if calendar_obj is None:
        return {
            "ok": False,
            "tenant": tenant,
            "hours_mode": "wall-clock",
            "error": error or f"no calendar config at {CALENDAR_CONFIG.name}",
            "sources": [str(CALENDAR_CONFIG)],
        }
    return {"ok": True, "tenant": tenant, **calendar_obj.summary()}


@app.get("/api/events")
def events(request: Request):
    """A page of the event log.

    ``limit``/``offset`` page it (GAP-04); ``X-Total-Count`` says how many rows
    match so a client never has to download them to count them. Filters are
    real: ``repo``, ``actor``, ``activity``, ``ticket`` and ``pr_id`` are
    applied in SQL, and anything not listed is a 400 (BUG-13).
    """
    unknown = _unknown_params(request, _params(query.EVENT_PARAMS))
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
        return _bad_request(str(exc), valid=sorted(_params(query.EVENT_PARAMS)))
    activity = request.query_params.get("activity") or None
    if activity is not None and activity not in taxonomy.ACTIVITIES:
        return _bad_request(
            f"activity {activity!r} is outside the closed vocabulary",
            valid=sorted(taxonomy.ACTIVITIES),
        )
    order = (request.query_params.get("order") or "asc").lower()
    if order not in ("asc", "desc"):
        return _bad_request("order must be 'asc' or 'desc'", valid=["asc", "desc"])
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
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
            "X-Tenant": tenant,
        },
    )


@app.get("/api/events/counts")
def event_counts(request: Request):
    """Per-bucket event counts plus window totals — no per-event rows (GAP-04)."""
    unknown = _unknown_params(request, _params(query.COUNT_PARAMS))
    if unknown is not None:
        return unknown
    try:
        start, end = query.parse_range(
            request.query_params.get("from"), request.query_params.get("to")
        )
        pr_id = _int_param(request, "pr_id", default=None, minimum=0)
    except ValueError as exc:
        return _bad_request(str(exc), valid=sorted(_params(query.COUNT_PARAMS)))
    activity = request.query_params.get("activity") or None
    if activity is not None and activity not in taxonomy.ACTIVITIES:
        return _bad_request(
            f"activity {activity!r} is outside the closed vocabulary",
            valid=sorted(taxonomy.ACTIVITIES),
        )
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            counts = query.event_counts(
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
    return {**counts, "tenant": tenant}


@app.get("/api/objects")
def objects(request: Request):
    """A page of the object table; filterable by type/repo/pr_id/actor/ticket."""
    unknown = _unknown_params(request, _params(query.OBJECT_PARAMS))
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
        return _bad_request(str(exc), valid=sorted(_params(query.OBJECT_PARAMS)))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
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
        headers={
            "X-Total-Count": str(total),
            "X-Limit": str(limit or total),
            "X-Offset": str(offset or 0),
            "X-Tenant": tenant,
        },
    )


# A ticket key as the store writes it (``DEL-167``). The endpoint accepts the
# object-id forms too, but a path segment is never a query, so validating the
# shape keeps a stray ``/api/ticket/../../x`` from reading as a lookup.
TICKET_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,9}-\d{1,6}$")


@app.get("/api/ticket/{key}")
def ticket_detail(key: str, request: Request):
    """One ticket's whole cycle (prd/25): the question a PR row cannot answer.

    The payload joins the STORY object, its segments (eight, or nine when the
    workflow has a prd/29 verification stage) with the boundary timestamps, its
    PRs (joined on ``PR.ticket_keys``) with review rounds / ``push_class`` /
    censoring, its sprint memberships, its worklogs and its events — plus a
    one-sentence ``answer`` that either says where the time went or names the
    boundary the store is missing. A key the store has never seen is a 404; a
    story without PRs (or a PR without a story) is a real, partially measured
    ticket and says which half is missing.

    The answer is cached per ``(tenant, store + calendar version, key)`` — the
    same invalidation as the ballcourt cache — and the Jira layer it is built
    from is cached once per store version (``_analysis_bundle``), not rebuilt
    per key: a new key used to cost 300–418 ms of ``load_jira`` at 4,000
    stories.
    """
    unknown = _unknown_params(request, _params(()))
    if unknown is not None:
        return unknown
    wanted = ticket_view.normalize_key(key)
    if not TICKET_KEY_RE.match(wanted):
        return _bad_request(
            f"ticket key {key!r} is not shaped like PROJ-123",
            valid=["a key such as DEL-167 (the STORY:/TICKET: prefixes are accepted too)"],
        )
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            payload = _ticket_payload(con, tenant, path, wanted)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    if payload is None:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": f"unknown ticket {wanted}",
                "tenant": tenant,
                "hint": "a ticket is a STORY object or a PR.ticket_keys entry; "
                        "this key appears in neither in this dataset",
            },
        )
    return JSONResponse(payload, headers={"X-Tenant": tenant})


# One ticket payload is a pure read of (store version, calendar version, key):
# ``tickets.ticket`` is 0.3–4.4 ms, but the Jira layer it needs was ~410 ms to
# rebuild at 4,000 stories — and that layer is per *store*, not per key. The
# payload cache is now a thin wrapper over the shared analysis bundle; the
# expensive part is built once per store version and every key reuses it.
# Bounded like the simulation cache; a store or calendar change makes the old
# entries unreachable, so they age out by size.
_ticket_lock = threading.Lock()
_ticket_cache: dict[tuple, dict | None] = {}
TICKET_CACHE_SIZE = 256


def _ticket_payload(con, tenant: str, path: Path, key: str) -> dict | None:
    """One ticket's payload (``None`` when the key is unknown), cached by version."""
    cache_key = (tenant, _file_stamp(path), _calendar_key(tenant), key)
    with _ticket_lock:
        if cache_key in _ticket_cache:
            return _ticket_cache[cache_key]
    bundle = _analysis_bundle(con, tenant, path)
    payload = ticket_view.ticket(
        bundle.court, bundle.jira(con), key, threads=bundle.threads(con),
        benchmarks=bundle.benchmarks(con),
    )
    if payload is not None:
        payload["tenant"] = tenant
        # prd/44 DES-5: the ticket's business-hour segments name their calendar.
        payload["calendar_id"] = bundle.court.calendar_id
    with _ticket_lock:
        if len(_ticket_cache) >= TICKET_CACHE_SIZE:
            _ticket_cache.clear()
        _ticket_cache[cache_key] = payload
    return payload


# --------------------------------------------------------------------------- #
# analysis views (prd/05): read-only projections of the same event log
# --------------------------------------------------------------------------- #
class _AnalysisBundle:
    """One store version's analysis inputs, built once and shared.

    The ballcourt, the Jira layer (``load_jira``), the review threads, the bot
    set and the PR sizes are all pure reads of *one store version* — none of
    them depends on the ticket key, the window or the endpoint. They used to be
    loaded per request (and ``load_jira`` per ticket *key*, so every new ticket
    paid ~0.3 s at 4,000 stories, and ``/api/insights`` paid it again on every
    call). Now the first analysis request for a store builds the court and each
    later request — any key, any endpoint — reuses it; the other slots are
    filled on first use so a page that never asks for the Jira layer never pays
    for it.

    The invalidation is the bundle's own key: ``(tenant, store stamp, calendar
    key)``, exactly the store-stamp + calendar identity the ballcourt cache has
    always used, so an ingest run or a calendar edit drops the whole bundle.

    Everything downstream treats the bundle as read-only — the one write is
    ``Story._cycle_cache``, an idempotent per-story memo of a pure function —
    so sharing it between requests and threads cannot change an answer.
    """

    __slots__ = ("court", "_lock", "_jira", "_threads", "_bots", "_sizes",
                 "_benchmarks")

    def __init__(self, court) -> None:
        self.court = court
        # One lock for every slot: a slot is filled at most once per store
        # version, and serialising the first build keeps a burst of concurrent
        # requests from all paying the same 0.3 s.
        self._lock = threading.Lock()
        self._jira = None
        self._threads = None
        self._bots = None
        self._sizes = None
        self._benchmarks = None

    def jira(self, con) -> jira_analysis.JiraData:
        """The Jira half of the store, joined to this bundle's court."""
        with self._lock:
            if self._jira is None:
                self._jira = insights_rules.load_jira(con, self.court)
            return self._jira

    def threads(self, con) -> list[dict]:
        with self._lock:
            if self._threads is None:
                self._threads = insights_rules.load_threads(con)
            return self._threads

    def bots(self, con) -> set[str]:
        with self._lock:
            if self._bots is None:
                self._bots = insights_rules.load_bots(con)
            return self._bots

    def sizes(self, con) -> dict[str, dict]:
        with self._lock:
            if self._sizes is None:
                self._sizes = insights_rules.load_pr_sizes(con)
            return self._sizes

    def benchmarks(self, con) -> dict:
        """prd/57: the per-duration cohorts, built once per store version.

        Segment medians need every completed story's segments, so this is the
        expensive slot — exactly the reason it lives on the bundle rather than
        being recomputed for a new ticket key.
        """
        jira = self.jira(con)
        with self._lock:
            if self._benchmarks is None:
                self._benchmarks = ticket_view.benchmark_cohorts(
                    self.court, jira,
                )
            return self._benchmarks


def _analysis_bundle(con, tenant: str, path: Path) -> _AnalysisBundle:
    """The analysis inputs of one (tenant, store + calendar version).

    One entry, not a dict: switching tenants rebuilds, but page switches within
    a tenant never do. The stamp is per file, so another tenant's ingest run
    cannot invalidate this one — and the calendar files are part of the key, so
    editing ``data/<tenant>/calendar.yaml`` takes effect without a restart.

    ``MEGASPEED_SENTINEL_AFTER_PEERS`` (prd/40) is process-level configuration,
    read once per build: ``0``/``false``/``off``/``no`` turns the sentinel gate
    off (the sentinel reviews in parallel with peers). It is deliberately not
    part of the cache key — it is not a per-request value, and a restart is the
    honest way to change it.
    """
    global _analysis_cache
    key = (tenant, _file_stamp(path), _calendar_key(tenant))
    with _analysis_lock:
        if _analysis_cache is not None and _analysis_cache[0] == key:
            return _analysis_cache[1]
    calendar, error = _calendar_for(tenant)
    sentinel_after_peers = os.environ.get(
        "MEGASPEED_SENTINEL_AFTER_PEERS", ""
    ).strip().lower() not in {"0", "false", "off", "no"}
    bundle = _AnalysisBundle(analyse.load(
        con, calendar=calendar, calendar_error=error,
        sentinel_after_peers=sentinel_after_peers,
    ))
    with _analysis_lock:
        _analysis_cache = (key, bundle)
    return bundle


def _ballcourt(con, tenant: str, path: Path):
    """The interval timeline of one (tenant, store + calendar version).

    A thin reader of :func:`_analysis_bundle`, kept for the views that need the
    court and nothing else.
    """
    return _analysis_bundle(con, tenant, path).court


# --------------------------------------------------------------------------- #
# business-hours calendar (prd/19): per tenant, cached by file version
# --------------------------------------------------------------------------- #
_calendar_lock = threading.Lock()
_calendar_cache: dict[str, tuple] = {}


def _tenant_calendar_path(tenant: str) -> Path:
    return TENANTS_DIR / tenant / workcal.TENANT_FILE


def _calendar_key(tenant: str) -> tuple:
    """Identity of the effective calendar: the committed file + the tenant file."""
    tenant_file = _tenant_calendar_path(tenant)
    return (
        str(CALENDAR_CONFIG), _file_stamp(CALENDAR_CONFIG),
        str(tenant_file), _file_stamp(tenant_file),
    )


def _calendar_for(tenant: str) -> tuple[object | None, str | None]:
    """The tenant's effective calendar and, if its config is broken, why.

    A malformed calendar is never silently ignored (every number would be
    quietly wrong) and never a 500 either: the analysis falls back to
    wall-clock, says so in ``hours_mode``, and ``/api/calendar`` reports the
    error next to the file it came from.
    """
    key = _calendar_key(tenant)
    with _calendar_lock:
        hit = _calendar_cache.get(tenant)
    if hit is not None and hit[0] == key:
        return hit[1], hit[2]
    try:
        calendar, error = workcal.load(
            tenant=tenant, tenants_dir=TENANTS_DIR, config_path=CALENDAR_CONFIG
        ), None
    except workcal.CalendarError as exc:
        calendar, error = None, str(exc)
        if LOG:
            print(f"megaspeed: calendar config ignored: {error}", file=sys.stderr)
    with _calendar_lock:
        _calendar_cache[tenant] = (key, calendar, error)
    return calendar, error


def _calendar_brief(tenant: str) -> dict:
    """Enough calendar for a label: id, mode, timezone, window, counts, sources."""
    calendar, error = _calendar_for(tenant)
    if calendar is None:
        return {
            "hours_mode": "wall-clock",
            "calendar_id": "none",
            "error": error,
            "sources": [str(CALENDAR_CONFIG)],
        }
    summary = calendar.summary()
    return {
        "hours_mode": summary["hours_mode"],
        "calendar_id": summary["calendar_id"],
        "timezone": summary["timezone"],
        "working_hours": summary["working_hours"],
        "weekdays": summary["weekdays"],
        "holidays": summary["holidays"]["count"],
        "overrides": len(summary["overrides"]),
        "vacations": len(summary["vacations"]),
        "sources": summary["sources"],
        "assumption": summary["assumption"],
    }


def _days(days: float | None) -> float | None:
    if days is None:
        return None
    return max(0.1, min(float(days), 3650.0))


def _level(level: str) -> str:
    if level not in ("queue", "person"):
        raise ValueError("level must be 'queue' or 'person'")
    return level


def _insight_level(level: str) -> str:
    if level not in ("team", "person"):
        raise ValueError("level must be 'team' or 'person'")
    return level


ANALYSIS_PARAMS = {
    "blocking": ("level", "days", "repo"),
    "queues": ("level", "days", "bucket_hours", "repo"),
    "delivery": ("days", "repo"),
    "aging": ("kind", "stage", "only", "days", "repo", "limit", "thresholds"),
    "insights": ("level", "days", "size_commits", "size_lines", "repo"),
    "verdict": ("days", "repo"),
    "sprints": ("days",),
    "trend": ("metric", "bucket", "days", "repo"),
    "trend-compare": ("metric", "split", "bucket", "days", "repo"),
    "annotations": ("repo",),
    "simulate": ("days", "scenario", "seed", "tolerance", "replications", "repo"),
    "pr-size": ("size_commits", "size_lines"),
}


def _repo_param(request: Request, court) -> str | None:
    """The requested repo scope (prd/49), validated against the store's PRs.

    An unknown repo is a 400 listing the real ones, never an empty view: a typo
    that silently answers "0 open items" looks like a finding.
    """
    repo = (request.query_params.get("repo") or "").strip() or None
    if repo and repo not in court.repos:
        raise ValueError(
            f"unknown repo {repo}: repo must be one of "
            f"{', '.join(court.repos) or '(none in this store)'}"
        )
    return repo


def _repo_scope(court, jira, repo: str | None):
    """The court and ticket layer narrowed to one repo (prd/49)."""
    if not repo:
        return court, jira
    scoped_court = analyse.scope_to_repo(court, repo)
    scoped_jira = jira_analysis.scope_to_repo(jira, repo) if jira is not None else None
    return scoped_court, scoped_jira

# prd/39: the PR-size caps are a per-tenant calibration. They arrive as query
# parameters (the UI persists them per tenant, like the scope/timeframe), so the
# server stays a read-only projection: the rule re-evaluates at the requested
# caps without a settings write path.
SIZE_CAP_MIN = insights_rules.SIZE_CAP_MIN
SIZE_CAP_MAX = insights_rules.SIZE_CAP_MAX


def _size_caps(request: Request) -> dict:
    """The requested PR-size caps; ``None`` keys fall back to the defaults."""
    commits = _int_param(
        request, "size_commits", default=None,
        minimum=SIZE_CAP_MIN, maximum=SIZE_CAP_MAX,
    )
    lines = _int_param(
        request, "size_lines", default=None,
        minimum=SIZE_CAP_MIN, maximum=SIZE_CAP_MAX,
    )
    return insights_rules.size_caps({"commits": commits, "lines": lines})


@app.get("/api/blocking")
def blocking(request: Request):
    """Weighted directed handover graph: ``X -> Y`` = X waited on Y for N hours."""
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["blocking"]))
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
        level = _level(request.query_params.get("level") or "queue")
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["blocking"])))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            bundle = _analysis_bundle(con, tenant, path)
            try:
                repo = _repo_param(request, bundle.court)
            except ValueError as exc:
                return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["blocking"])))
            court, _jira = _repo_scope(bundle.court, None, repo)
            window = analyse.window_from_events(court, _days(days))
            payload = analyse.blocking_graph(court, level, window)
            # prd/44 DES-5: which calendar priced these hours (re-derivable).
            payload["calendar_id"] = court.calendar_id
            # prd/49: the slice this answer describes (absent = whole store).
            payload["repo"] = repo
            return payload
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


@app.get("/api/queues")
def queues(request: Request):
    """Per-person / per-queue WIP over time, arrivals and completions."""
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["queues"]))
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
        bucket_hours = _num_param(request, "bucket_hours", minimum=1, maximum=720)
        level = _level(request.query_params.get("level") or "person")
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["queues"])))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            bundle = _analysis_bundle(con, tenant, path)
            try:
                repo = _repo_param(request, bundle.court)
            except ValueError as exc:
                return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["queues"])))
            jira = bundle.jira(con)
            court, jira = _repo_scope(bundle.court, jira, repo)
            window = analyse.window_from_events(court, _days(days))
            payload = analyse.queue_series(court, level, window, bucket_hours)
            # prd/42 M7: a pseudonym legend by role (sentinel / bot / author /
            # reviewer / story owner) — never a real name (prd/02). A repo scope
            # lists only the people who act in that repo (prd/49).
            payload["legend"] = analyse.person_legend(court, jira.stories)
            # prd/44 DES-5: the calendar identity travels with the hours.
            payload["calendar_id"] = court.calendar_id
            payload["repo"] = repo
            return payload
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


@app.get("/api/delivery")
def delivery(request: Request):
    """Triage list: open PRs, what they wait on, and for how long.

    prd/40: a row's ``ball`` is the derived label (``author`` | ``review`` |
    ``ready``), ``waiting_on`` is the truth (the pending reviewers and, while a
    reviewer's objection stands, the author), ``doubly_blocked`` says both
    dimensions are open, and ``ready_since`` is set when every reviewer has
    answered and only the merge click remains.
    """
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["delivery"]))
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["delivery"])))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            bundle = _analysis_bundle(con, tenant, path)
            try:
                repo = _repo_param(request, bundle.court)
            except ValueError as exc:
                return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["delivery"])))
            # prd/34 §5: "ticket Done but PR open" needs the ticket layer's end
            # state; the bundle caches it, so this costs one load per version.
            jira = bundle.jira(con)
            court, jira = _repo_scope(bundle.court, jira, repo)
            window = analyse.window_from_events(court, _days(days))
            done = jira_analysis.done_keys(jira)
            payload = analyse.delivery_list(court, window, done_tickets=done)
            # prd/42 M4: a first-class list of Done-in-Jira tickets with an
            # open PR, with who owes each — the QA run had to reconstruct it.
            payload["done_open"] = ticket_view.done_but_open(court, jira, payload)
            # prd/42 M6: the PR drawer shows open threads instead of a dash.
            open_threads: dict[tuple, int] = {}
            for thread in bundle.threads(con):
                if thread.get("closed_at") is not None:
                    continue
                pair = (thread.get("repo"), thread.get("pr_id"))
                open_threads[pair] = open_threads.get(pair, 0) + 1
            for row in payload.get("rows") or ():
                row["open_threads"] = open_threads.get((row["repo"], row["number"]), 0)
            # prd/44 DES-5: the calendar identity travels with the hours.
            payload["calendar_id"] = court.calendar_id
            payload["repo"] = repo
            return payload
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)


# --------------------------------------------------------------------------- #
# aging WIP (prd/59): in-flight age vs the team's own percentiles + the chase
# --------------------------------------------------------------------------- #
_aging_lock = threading.Lock()
_aging_cache: dict[tuple, dict] = {}
_aging_threshold_cache: dict[tuple, dict] = {}
AGING_CACHE_SIZE = 32


def _aging_thresholds(tenant: str, path: Path, court, jira, window,
                      repo: str | None, mode: str) -> dict:
    """The two rule thresholds, cached per (store, calendar, window, repo).

    Thresholds depend only on the cohort and the window, so ``/api/aging``,
    ``/api/insights`` (the rule evidence) and prd/58's exploit pool share one
    computation instead of three.
    """
    key = (
        tenant, str(path), _file_stamp(path), _calendar_key(tenant),
        window[0], window[1], repo or "", mode,
    )
    with _aging_lock:
        hit = _aging_threshold_cache.get(key)
    if hit is not None:
        return hit
    payload = aging_view.rule_thresholds(court, jira, window, mode=mode)
    with _aging_lock:
        if len(_aging_threshold_cache) >= AGING_CACHE_SIZE:
            _aging_threshold_cache.clear()
        _aging_threshold_cache[key] = payload
    return payload


@app.get("/api/aging")
def aging(request: Request):
    """In-flight age against the cohort's own p50/p85/p95, and the chase list.

    Every row states which clock priced it (story cycle, story stage, PR review
    or PR cycle); the two clocks are never summed. ``kind=story|pr`` and
    ``stage`` pick the cohort, ``only=live|all`` hides abandoned work (the
    Overview tile arrives with ``all`` so the dead items are seen once), and
    ``thresholds=self|fixed`` restores the old 5 bd and 24 h constants. An
    unknown stage is a 400 listing the stages this store can answer.

    ``feed`` is the chase data source prd/54 and prd/58 read: ``chase`` is the
    ranked list of live items past the cohort's p85, ``hours_at_risk`` the
    removable wait it carries, and ``replan`` the abandoned items (no live PR —
    close or re-plan, never chase).
    """
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["aging"]))
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
        limit = _int_param(request, "limit", default=None, minimum=1, maximum=1000)
        kind = (request.query_params.get("kind") or "story").strip()
        if kind not in aging_view.KINDS:
            raise ValueError(
                f"kind must be one of {', '.join(aging_view.KINDS)}"
            )
        only = (request.query_params.get("only") or "all").strip()
        if only not in ("live", "all"):
            raise ValueError("only must be 'live' or 'all'")
        mode = aging_view.normalize_mode(request.query_params.get("thresholds"))
        stage = (request.query_params.get("stage") or "").strip() or None
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["aging"])))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            bundle = _analysis_bundle(con, tenant, path)
            try:
                repo = _repo_param(request, bundle.court)
            except ValueError as exc:
                return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["aging"])))
            jira = bundle.jira(con)
            court, jira = _repo_scope(bundle.court, jira, repo)
            window = aging_view.window_for(court, jira, _days(days))
            key = (
                tenant, str(path), _file_stamp(path), _calendar_key(tenant),
                window[0], window[1], repo or "", kind, stage or "", only,
                limit or 0, mode,
            )
            with _aging_lock:
                hit = _aging_cache.get(key)
            if hit is not None:
                return JSONResponse(hit, headers={"X-Tenant": tenant})
            payload = aging_view.payload(
                court, jira, kind=kind, stage=stage, only=only, limit=limit,
                window=window, mode=mode,
            )
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["aging"])))
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    payload.update({
        "tenant": tenant,
        "repo": repo,
        "calendar_id": court.calendar_id,
        "assumption": court.assumption,
        **court.calendar_warning(),
        "thresholds_mode": mode,
        "rule_thresholds": _aging_thresholds(
            tenant, path, court, jira, window, repo, mode,
        ),
    })
    with _aging_lock:
        if len(_aging_cache) >= AGING_CACHE_SIZE:
            _aging_cache.clear()
        _aging_cache[key] = payload
    return JSONResponse(payload, headers={"X-Tenant": tenant})


# prd/43 §1: the Rules ↔ Policy-lab cross-check. It runs the lab's *preset*
# scenarios (not one per rule — that is the larger follow-up the PRD defers), so
# five forecast runs per (store, calendar, window) are cached and every Rules
# request after the first reuses them. A store or calendar change makes the old
# entries unreachable.
_advice_lock = threading.Lock()
_advice_cache: dict[tuple, dict] = {}
ADVICE_CACHE_SIZE = 32


def _advice_payload(con, tenant: str, path: Path, court, jira, window, repo: str | None = None) -> dict:
    key = (
        tenant, str(path), _file_stamp(path), _calendar_key(tenant),
        window[0], window[1], repo or "",
    )
    with _advice_lock:
        hit = _advice_cache.get(key)
    if hit is not None:
        return hit
    try:
        refinement = jira_analysis.refinement_cost(court, jira, window)
        calibration = policy_lab.calibrate(
            court, window, conn=con, refinement=refinement,
        )
        # prd/62: the measured stability baselines the model does not simulate.
        # Passing them here means the Rules note and the Policy lab row quote one
        # source, and a guardrail cannot be held at a number the panel does not
        # show.
        stability = jira_analysis.stability(court, jira, window)
        comparisons: dict[str, dict | None] = {}
        if calibration.get("ok", True):
            for policy in advice_crosscheck.POLICIES:
                scenario = policy_lab.Scenario.from_dict(policy["scenario"])
                result = policy_lab.simulate(
                    calibration, scenario, held_constant=stability,
                )
                comparisons[policy["id"]] = result.get("compare")
        report = (
            advice_crosscheck.report(comparisons)
            if comparisons else
            advice_crosscheck.unavailable(
                calibration.get("note")
                or "the policy lab could not calibrate a model from this store"
            )
        )
        # prd/62: the lab-implied per-step yield, for the panel's warning dot.
        # The model fits a rework probability (share of PRs with more than one
        # push), so its implied clean-review share is its complement; it does
        # not model the other three steps and the payload says so.
        probability = ((calibration or {}).get("rework") or {}).get("probability")
        if report.get("available") and probability is not None:
            report["implied_yield"] = {
                "steps": {"review": round(1.0 - float(probability), 3)},
                "basis": (
                    "the lab's fitted rework probability (share of PRs with "
                    "more than one push); it does not model the other steps"
                ),
            }
    except Exception as exc:  # noqa: BLE001 - the cross-check must not break Rules
        # A store the model cannot calibrate (or a SimPy edge case) is a stated
        # no-result, not a 503: the measured rules still answer, and the page
        # says why the lab did not.
        report = advice_crosscheck.unavailable(
            f"the policy lab could not run on this store: {exc}"
        )
    with _advice_lock:
        if len(_advice_cache) >= ADVICE_CACHE_SIZE:
            _advice_cache.clear()
        _advice_cache[key] = report
    return report


@app.get("/api/insights")
def insights(request: Request):
    """Ranked "change this" rules with evidence and severity (prd/17).

    ``level=team`` is the flow tax (no names); ``level=person`` adds per-person
    rules, which carry a pseudonym in ``scope`` and nothing else. Ranking is
    impact hours × confidence, and ``hours_mode`` is ``business`` when the
    tenant's calendar applies (prd/19), ``wall-clock`` otherwise.

    The ticket layer (prd/21) rides along: ``jira`` says whether the store has
    one, and the decomposition / carry-over / churn / worklog / conformance
    metrics and their rules are added when it does. A store without the Jira
    ingest reports ``available: false`` on each of them instead of zeroes.

    prd/37: ``at_stake`` is the additive headline — each cost pool is counted
    once, at its most specific rule — and every suggestion carries its
    ``at_stake`` attribution, so the list cannot be added up twice.

    prd/43: ``policy_lab`` is the Rules ↔ Policy-lab cross-check — the lab's own
    preset results (cached per store/calendar/window), the note each mapped rule
    carries, and the lab's best scenario. A store the model cannot validate
    reports ``available: false`` with the reason; the measured rules still show.

    prd/51: ``rework`` is the story-level rework aggregate — reopened, failed
    verification, review bounces and approved-but-never-merged PRs, each row a
    ticket key with its count and hours (derived from the status history and the
    PR status vectors, over the whole store).
    """
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["insights"]))
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
        level = _insight_level(request.query_params.get("level") or "team")
        caps = _size_caps(request)
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["insights"])))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            bundle = _analysis_bundle(con, tenant, path)
            try:
                repo = _repo_param(request, bundle.court)
            except ValueError as exc:
                return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["insights"])))
            # prd/21: the ticket layer is loaded before the window, because a
            # Jira-only store's earliest item is a story, not a PR.
            jira = bundle.jira(con)
            # prd/49: a repo-scoped rule recomputes against that repo's cohort —
            # both the PR side and the stories joined to its PRs.
            court, jira = _repo_scope(bundle.court, jira, repo)
            window = insights_rules.window_for(court, _days(days), jira)
            threads = bundle.threads(con)
            bots = bundle.bots(con)
            sizes = bundle.sizes(con)
            cross_check = _advice_payload(
                con, tenant, path, court, jira, window, repo,
            )
            payload = insights_rules.insights(
                court, threads, window, level=level, bots=bots, sizes=sizes,
                jira=jira, caps=caps,
                policy_lab=cross_check,
                # prd/62: the lab-implied step yield (warning dot only); the
                # measured %C&A never comes from the model.
                implied=(cross_check or {}).get("implied_yield"),
                # prd/59: the rules quote the same self-calibrated thresholds
                # the Aging tab shows, from one cached computation.
                aging=_aging_thresholds(
                    tenant, path, court, jira, window, repo,
                    aging_view.thresholds_mode(),
                ),
            )
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    payload["tenant"] = tenant
    payload["calendar_id"] = court.calendar_id
    payload["repo"] = repo
    # prd/53 §2: the pseudonym legend rides with the rules too, so "who is the
    # sentinel" is answerable next to the worklog card without visiting Queue.
    payload["legend"] = analyse.person_legend(court, jira.stories if jira else None)
    return payload


# prd/58: the constraint verdict. One entry per (tenant, store, calendar,
# window, repo, advice window). The heavy inputs — the ball court, the ticket
# layer and the prd/43 advice report — are already cached per (store, calendar,
# window), so this endpoint adds one cohort pass and one rules pass, not five
# simulations. A new ingest or a calendar edit makes the old key unreachable.
_verdict_lock = threading.Lock()
_verdict_cache: dict[tuple, dict] = {}
VERDICT_CACHE_SIZE = 32


@app.get("/api/verdict")
def verdict(request: Request):
    """The one current constraint, its holders, and the exploit → elevate ladder.

    prd/58: the station with the largest **removable** pool (live in-station wait
    + the completed stories' station hours above the station's own p85), on the
    story clock. The review detail's holders are the PR ball clock and are never
    summed with it. Every rung carries the policy lab's verdict from the *same*
    cached prd/43 advice payload ``/api/insights`` uses, and ``elevate`` is
    ``blocked`` while ``exploit`` items are still owed.

    ``available: false`` with a stated reason (never a zero-filled card) when the
    window has fewer than 20 completed stories or the largest pool is under one
    waiting week; the full ranking still travels in ``pools``. An unknown repo is
    a 400 listing the store's repos, never an empty verdict.
    """
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["verdict"]))
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["verdict"])))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            bundle = _analysis_bundle(con, tenant, path)
            try:
                repo = _repo_param(request, bundle.court)
            except ValueError as exc:
                return _bad_request(
                    str(exc), valid=list(_params(ANALYSIS_PARAMS["verdict"])),
                )
            jira = bundle.jira(con)
            court, jira = _repo_scope(bundle.court, jira, repo)
            # The same window /api/insights uses, so the advice cache entry (and
            # the lab note on each rung) is literally the same object.
            window = insights_rules.window_for(court, _days(days), jira)
            key = (
                tenant, str(path), _file_stamp(path), _calendar_key(tenant),
                days, repo or "", pstore.iso(window[1]),
            )
            with _verdict_lock:
                hit = _verdict_cache.get(key)
            if hit is not None:
                return JSONResponse(hit, headers={"X-Tenant": tenant})
            cross_check = _advice_payload(
                con, tenant, path, court, jira, window, repo,
            )
            # The ladder needs the *ranked* suggestions (titles, impact hours),
            # exactly the list Rules shows. No new metric is computed here.
            insights = insights_rules.insights(
                court, bundle.threads(con), window, level="team",
                bots=bundle.bots(con), sizes=bundle.sizes(con), jira=jira,
                policy_lab=cross_check,
                implied=(cross_check or {}).get("implied_yield"),
                aging=_aging_thresholds(
                    tenant, path, court, jira, window, repo,
                    aging_view.thresholds_mode(),
                ),
            )
            payload = verdict_view.verdict(
                court, jira, window, insights.get("suggestions") or (),
                cross_check,
            )
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    payload.update({
        "tenant": tenant,
        "repo": repo,
        "assumption": court.assumption,
        **court.calendar_warning(),
    })
    with _verdict_lock:
        if len(_verdict_cache) >= VERDICT_CACHE_SIZE:
            _verdict_cache.clear()
        _verdict_cache[key] = payload
    return JSONResponse(payload, headers={"X-Tenant": tenant})


@app.get("/api/pr-size")
def pr_size(request: Request):
    """The two PR-size distributions behind the size caps (prd/39).

    ``commits`` is a histogram of commits per PR (COMMIT objects, the same count
    the size rule uses); ``lines`` is the changed-lines histogram when the store
    captured ``lines_added``/``lines_removed`` (``available: false`` otherwise).
    Each carries bucket ``edges`` + ``counts``, the sorted raw ``values`` (so the
    UI can re-bucket exactly at any cap without a request), and the maximum.
    ``caps`` is the calibration this answer was computed at (``size_commits`` /
    ``size_lines`` override the 10 / 400 defaults), and ``large_prs`` is the
    count of PRs over either cap — the number the sliders move.

    Tenant-scoped and cached like the other analysis views: the cache key is
    ``(tenant, store version, calendar version, caps)``, so a slider move is one
    cheap re-read and an ingest run or calendar edit drops it.
    """
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["pr-size"]))
    if unknown is not None:
        return unknown
    try:
        caps = _size_caps(request)
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["pr-size"])))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            payload = _pr_size_payload(con, tenant, path, caps)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    return JSONResponse(payload, headers={"X-Tenant": tenant})


# One payload per (tenant, store + calendar version, caps). The distributions
# themselves do not depend on the caps, but the "large" flag and count do; the
# entry is tiny (histograms + sorted values), so keying on the caps is simpler
# than maintaining two layers. A store/calendar change makes old entries
# unreachable, so they age out by size.
_pr_size_lock = threading.Lock()
_pr_size_cache: dict[tuple, dict] = {}
PR_SIZE_CACHE_SIZE = 64


def _pr_size_payload(con, tenant: str, path: Path, caps: dict) -> dict:
    cache_key = (
        tenant, _file_stamp(path), _calendar_key(tenant),
        caps["commits"], caps["lines"],
    )
    with _pr_size_lock:
        if cache_key in _pr_size_cache:
            return _pr_size_cache[cache_key]
    bundle = _analysis_bundle(con, tenant, path)
    court = bundle.court
    jira = bundle.jira(con)
    window = insights_rules.window_for(court, None, jira)
    payload = insights_rules.pr_size_distribution(
        court, window, bundle.sizes(con), caps=caps,
    )
    start, end = window
    payload.update({
        "ok": True,
        "tenant": tenant,
        "as_of": pstore.iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "calendar_id": court.calendar_id,
        "assumption": court.assumption,
        "window": {
            "start": pstore.iso(start),
            "end": pstore.iso(end),
            "days": round(max(0.0, (end - start).total_seconds()) / 86400.0, 1),
        },
    })
    with _pr_size_lock:
        if len(_pr_size_cache) >= PR_SIZE_CACHE_SIZE:
            _pr_size_cache.clear()
        _pr_size_cache[cache_key] = payload
    return payload


@app.get("/api/sprints")
def sprints(request: Request):
    """The sprint lens (prd/26 P6): bands, scope and carry-over.

    ``sprints`` is one band per SPRINT object over its measured window (start →
    completion, or start → snapshot while active; ``planned_end`` is the plan,
    never the band). ``scope`` counts the stories created in the window that
    were committed to a sprint against the ones that were not, and lists the
    unplanned keys. Each band carries the prd/37 ``done_by_end`` / ``done_late``
    / ``never_done`` breakdown measured at its close, and ``delivery`` is the
    per-sprint on-time verdict. ``carry_over`` is "committed to a closed sprint
    but not done by its close" (late or still open), not only an explicit
    re-add. A store with no Jira layer answers ``available: false`` with the
    reason, not zeroes.

    prd/52: ``at_risk`` is the **active** sprint's forecast — each story the
    sprint's own done-by-snapshot count does not deliver, classified as
    done-but-unmerged / stuck-on / approved-unmerged / no-live-pr, with who owes
    the wait and for how long. A store with no active sprint reports
    ``available: false`` with the reason.
    """
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["sprints"]))
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["sprints"])))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            bundle = _analysis_bundle(con, tenant, path)
            court = bundle.court
            jira = bundle.jira(con)
            window = insights_rules.window_for(court, _days(days), jira)
            payload = jira_analysis.sprints(court, jira, window)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    start, end = window
    payload.update({
        "ok": True,
        "tenant": tenant,
        "as_of": pstore.iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "calendar_id": court.calendar_id,
        "assumption": court.assumption,
        **court.calendar_warning(),
        "jira": jira.summary(),
        "window": {
            "start": pstore.iso(start),
            "end": pstore.iso(end),
            "days": round(max(0.0, (end - start).total_seconds()) / 86400.0, 1),
        },
    })
    return JSONResponse(payload, headers={"X-Tenant": tenant})


# --------------------------------------------------------------------------- #
# trend / time series (prd/50): the window metrics, one bucket at a time
# --------------------------------------------------------------------------- #
# One payload per (tenant, store + calendar version, window, bucket, metric).
# A trend is several evaluations of the same metric functions, so the answer is
# cached exactly like the other analysis views; a store or calendar change
# makes old entries unreachable.
_trend_lock = threading.Lock()
_trend_cache: dict[tuple, dict] = {}
TREND_CACHE_SIZE = 32


@app.get("/api/trend")
def trend(request: Request):
    """One headline metric as a time series, bucketed by week or sprint.

    ``metric`` is one of ``flow_efficiency`` (default), ``cycle_median``,
    ``on_time``, ``wait_hours``, ``merged``, ``open``; ``bucket`` is ``week``
    (default, Monday-aligned) or ``sprint``. Each point is **the same metric
    function the headline uses**, evaluated on the bucket's own window with the
    same cohort rule and calendar — no new definitions (prd/50). The bucket
    containing the snapshot is ``in_progress`` (its cohort is still
    accumulating), and the active sprint is named when the store has one. The
    payload carries the metric catalogue, its definition, and a summary
    (first → latest change), so the chart and the tab labels come from one
    answer. ``repo`` narrows the cohort exactly like the other analysis views
    (prd/49): the buckets are recomputed on that repo's PRs, never a silent
    whole-store series.
    """
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["trend"]))
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
        metric = (
            request.query_params.get("metric") or trend_view.DEFAULT_METRIC
        ).strip()
        bucket = (
            request.query_params.get("bucket") or trend_view.DEFAULT_BUCKET
        ).strip()
        if metric not in trend_view.METRICS:
            raise ValueError(f"metric must be one of {sorted(trend_view.METRICS)}")
        if bucket not in trend_view.BUCKETS:
            raise ValueError(f"bucket must be one of {sorted(trend_view.BUCKETS)}")
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["trend"])))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            bundle = _analysis_bundle(con, tenant, path)
            # prd/49 + prd/63 A1: the trend is a scope-aware view like every
            # other analysis endpoint. The repo is validated against the store
            # (an unknown one is a 400 listing the real ones) and the cohort is
            # narrowed before any bucket is evaluated, so `repo=` cannot 400 a
            # view that persists the scope.
            try:
                repo = _repo_param(request, bundle.court)
            except ValueError as exc:
                return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["trend"])))
            key = (
                tenant, _store_stamp(path), _calendar_key(tenant),
                days, bucket, metric, repo or "",
            )
            with _trend_lock:
                hit = _trend_cache.get(key)
            if hit is not None:
                return JSONResponse(hit, headers={"X-Tenant": tenant})
            jira = bundle.jira(con)
            court, jira = _repo_scope(bundle.court, jira, repo)
            window = insights_rules.window_for(court, _days(days), jira)
            payload = trend_view.trend(
                court, jira, window, metric=metric, bucket=bucket,
                # prd/61: the team's dated changes and the calendar's own spans.
                # Annotations live in the store, so the store stamp above already
                # invalidates this cached answer after a write.
                annotations=annotation_view.load(con, repo),
                spans=annotation_view.calendar_spans(court, window),
            )
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    payload.update({
        "ok": True,
        "tenant": tenant,
        "repo": repo,
        "as_of": pstore.iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "calendar_id": court.calendar_id,
        "assumption": court.assumption,
        **court.calendar_warning(),
        "jira": jira.summary(),
    })
    with _trend_lock:
        if len(_trend_cache) >= TREND_CACHE_SIZE:
            _trend_cache.clear()
        _trend_cache[key] = payload
    return JSONResponse(payload, headers={"X-Tenant": tenant})


# --------------------------------------------------------------------------- #
# before/after (prd/61): item-level distributions either side of a split
# --------------------------------------------------------------------------- #
# A compare is a bootstrap + rank test over item samples; it shares the trend's
# invalidation key (store stamp + calendar) and the same cache budget. The seed
# is derived from that key, so a cached and a cold call agree.
_compare_cache: dict[tuple, dict] = {}
COMPARE_CACHE_SIZE = 32


@app.get("/api/trend/compare")
def trend_compare(request: Request):
    """Did the change work? Item-level before/after around one dated split.

    ``metric`` is ``story_cycle`` or ``cycle_median``; ``split`` is the change
    date (ISO). Items created in the bucket containing the snapshot are excluded
    from both sides, open items are censored and counted, and the answer is one
    of ``better`` / ``worse`` / ``no clear change`` / ``can't tell at this n``.
    Never a bare p-value: both ``n``s and the CI travel with the verdict.
    """
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["trend-compare"]))
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
        metric = (
            request.query_params.get("metric") or "story_cycle"
        ).strip()
        bucket = (
            request.query_params.get("bucket") or trend_view.DEFAULT_BUCKET
        ).strip()
        if metric not in before_after_view.COMPARE_METRICS:
            raise ValueError(
                f"metric must be one of {sorted(before_after_view.COMPARE_METRICS)}"
            )
        if bucket not in trend_view.BUCKETS:
            raise ValueError(f"bucket must be one of {sorted(trend_view.BUCKETS)}")
        split = before_after_view.parse_split(request.query_params.get("split"))
    except ValueError as exc:
        return _bad_request(
            str(exc), valid=list(_params(ANALYSIS_PARAMS["trend-compare"]))
        )
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with store_conn(path) as con:
            bundle = _analysis_bundle(con, tenant, path)
            try:
                repo = _repo_param(request, bundle.court)
            except ValueError as exc:
                return _bad_request(
                    str(exc),
                    valid=list(_params(ANALYSIS_PARAMS["trend-compare"])),
                )
            key = (
                tenant, _store_stamp(path), _calendar_key(tenant),
                days, bucket, metric, split.isoformat(), repo or "",
            )
            with _trend_lock:
                hit = _compare_cache.get(key)
            if hit is not None:
                return JSONResponse(hit, headers={"X-Tenant": tenant})
            jira = bundle.jira(con)
            court, jira = _repo_scope(bundle.court, jira, repo)
            window = insights_rules.window_for(court, _days(days), jira)
            payload = before_after_view.compare_metric(
                court, jira, window,
                metric=metric, split=split, bucket=bucket,
                annotations=annotation_view.load(con, repo),
                tenant=tenant, stamp=_store_stamp(path),
                calendar_id=court.calendar_id,
            )
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    payload.update({
        "ok": True,
        "tenant": tenant,
        "repo": repo,
        "as_of": pstore.iso(court.snapshot),
        "hours_mode": court.hours_mode,
        "calendar_id": court.calendar_id,
        "assumption": court.assumption,
        "metrics": before_after_view.metric_ids(),
        **court.calendar_warning(),
    })
    with _trend_lock:
        if len(_compare_cache) >= COMPARE_CACHE_SIZE:
            _compare_cache.clear()
        _compare_cache[key] = payload
    return JSONResponse(payload, headers={"X-Tenant": tenant})


# --------------------------------------------------------------------------- #
# annotations (prd/61): the ONE first-party write path
# --------------------------------------------------------------------------- #
def _write_denied(request: Request) -> JSONResponse | None:
    """A browser write must be same-origin (prd/61's one write path).

    Reads are public on this local tool; a write is not. A cross-site page can
    make a browser POST/PUT/DELETE to ``127.0.0.1`` even though CORS stops it
    reading the answer, so the Origin is checked before anything is stored.
    ``curl`` (no Origin) keeps working, exactly like the rest of the suite.
    """
    if _origin_allowed(request.headers.get("origin"), request.headers.get("host")):
        return None
    return JSONResponse(
        status_code=403,
        content={"ok": False, "error": "cross-origin writes are not allowed"},
    )


def _annotation_write_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, (StoreReadOnly, PermissionError)):
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "unavailable": True,
                "error": "this store is read-only (a mirror install still reads annotations)",
            },
        )
    if isinstance(exc, StoreBusy):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "the store is being written right now",
                     "retryable": True},
            headers={"Retry-After": "1"},
        )
    return _store_error(exc)


@app.get("/api/annotations")
def annotations_list(request: Request):
    """The team's dated changes and the closed vocabulary (prd/61)."""
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["annotations"]))
    if unknown is not None:
        return unknown
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    repo = (request.query_params.get("repo") or "").strip() or None
    try:
        with store_conn(path) as con:
            entries = annotation_view.load(con, repo)
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    return JSONResponse(
        {
            "ok": True,
            "tenant": tenant,
            "repo": repo,
            "annotations": entries,
            "kinds": annotation_view.catalogue(),
        },
        headers={"X-Tenant": tenant},
    )


@app.post("/api/annotations")
async def annotations_create(request: Request):
    """Record one annotation. 400 only for the closed-vocabulary ``kind``.

    The optional ``note`` is first-party free text (the team's own words), stored
    verbatim in the allowlisted ``annotations`` table — never the event log,
    never the classifier (prd/02 "Scope").
    """
    denied = _write_denied(request)
    if denied is not None:
        return denied
    unknown = _unknown_params(request, _params(()))
    if unknown is not None:
        return unknown
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    body = await _json_body(request)
    if body is None:
        return _bad_request("invalid json")
    try:
        clean = annotation_view.validate(body)
    except annotation_view.AnnotationError as exc:
        return _bad_request(str(exc), valid=[kind["id"] for kind in annotation_view.catalogue()])
    try:
        with _annotation_write_lock:
            with store_write_conn(path) as con:
                con.execute("BEGIN")
                try:
                    entry = annotation_view.save(con, clean)
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
    except Exception as exc:  # noqa: BLE001
        return _annotation_write_error(exc)
    _bump_store_stamp(path)
    return JSONResponse(
        {"ok": True, "tenant": tenant, "annotation": entry},
        status_code=201,
        headers={"X-Tenant": tenant},
    )


@app.delete("/api/annotations/{annotation_id}")
def annotations_delete(annotation_id: str, request: Request):
    """Delete one annotation (a two-step inline confirm in the UI, prd/61)."""
    denied = _write_denied(request)
    if denied is not None:
        return denied
    unknown = _unknown_params(request, _params(()))
    if unknown is not None:
        return unknown
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    try:
        with _annotation_write_lock:
            with store_write_conn(path) as con:
                con.execute("BEGIN")
                try:
                    deleted = annotation_view.delete(con, annotation_id.strip())
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
    except Exception as exc:  # noqa: BLE001
        return _annotation_write_error(exc)
    if not deleted:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"no annotation {annotation_id!r} in this store"},
            headers={"X-Tenant": tenant},
        )
    _bump_store_stamp(path)
    return JSONResponse(
        {"ok": True, "tenant": tenant, "deleted": annotation_id},
        headers={"X-Tenant": tenant},
    )


# --------------------------------------------------------------------------- #
# policy lab (prd/23): calibrate the model from the store, run one scenario
# --------------------------------------------------------------------------- #
_sim_lock = threading.Lock()
_sim_cache: dict = {}
SIM_CACHE_SIZE = 64


@app.get("/api/simulate")
def simulate(request: Request):
    """Calibrated discrete-event model + validation gate + one scenario A/B.

    ``scenario`` is a JSON object of policy knobs (``wip_limit``, ``sentinels``,
    ``review_sla_hours``, ``size_cap_commits``, ``stack_depth_cap``,
    ``review_first``). Omitted means "baseline only". The response always
    carries ``validation``; when the baseline does not reproduce the historical
    cycle-time median within ``tolerance`` (±20 % by default) the scenario is
    **not run** and ``blocked`` says why — a confident-looking wrong number is
    worse than no number (prd/23 §"Validation gate").
    """
    unknown = _unknown_params(request, _params(ANALYSIS_PARAMS["simulate"]))
    if unknown is not None:
        return unknown
    try:
        days = _num_param(request, "days", minimum=0, maximum=3650)
        seed = _int_param(request, "seed", default=policy_lab.DEFAULT_SEED, minimum=0)
        replications = _int_param(
            request, "replications", default=policy_lab.DEFAULT_REPLICATIONS,
            minimum=1, maximum=policy_lab.MAX_REPLICATIONS,
        )
        tolerance = _num_param(
            request, "tolerance", default=policy_lab.DEFAULT_TOLERANCE,
            minimum=0.01, maximum=1.0,
        )
        raw = request.query_params.get("scenario")
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"scenario is not valid JSON ({exc.msg})") from None
            scenario = policy_lab.Scenario.from_dict(body)
        else:
            scenario = None
    except policy_lab.SimulationError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["simulate"])))
    except ValueError as exc:
        return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["simulate"])))
    try:
        tenant, path = _tenant_of(request)
    except UnknownTenant as exc:
        return _tenant_error(exc.args[0])
    key = (
        tenant, _file_stamp(path), _calendar_key(tenant), days,
        (request.query_params.get("repo") or "").strip(),
        raw or "", seed, replications, tolerance,
    )
    with _sim_lock:
        hit = _sim_cache.get(key)
    if hit is not None:
        return JSONResponse(hit)
    try:
        with store_conn(path) as con:
            bundle = _analysis_bundle(con, tenant, path)
            try:
                repo = _repo_param(request, bundle.court)
            except ValueError as exc:
                return _bad_request(str(exc), valid=list(_params(ANALYSIS_PARAMS["simulate"])))
            # the calibration excludes the store's bot actors from the reviewer
            # pool by default (FR-7): a service account is not capacity to staff
            jira = bundle.jira(con)
            # prd/49: the lab calibrates on the repo's own cohort.
            court, jira = _repo_scope(bundle.court, jira, repo)
            window = analyse.window_from_events(court, _days(days))
            refinement = jira_analysis.refinement_cost(court, jira, window)
            calibration = policy_lab.calibrate(
                court, window, conn=con, refinement=refinement,
            )
            # prd/62: the measured stability panel travels with the comparison so
            # the guardrail's held-constant baselines are the panel's numbers —
            # measured on the Rules page's own window, so the two surfaces agree.
            stability = jira_analysis.stability(
                court, jira, insights_rules.window_for(court, _days(days), jira)
            )
            payload = policy_lab.simulate(
                calibration, scenario, seed=seed, tolerance=tolerance,
                replications=replications, held_constant=stability,
            )
    except Exception as exc:  # noqa: BLE001
        return _store_error(exc)
    payload["tenant"] = tenant
    payload["calendar_id"] = court.calendar_id
    payload["repo"] = repo
    payload["params"] = {
        "days": days, "seed": seed, "replications": replications,
        "tolerance": tolerance,
        "repo": repo,
        "scenario": scenario.as_dict() if scenario else None,
    }
    with _sim_lock:
        if len(_sim_cache) >= SIM_CACHE_SIZE:
            _sim_cache.clear()
        _sim_cache[key] = payload
    return JSONResponse(payload)


# --------------------------------------------------------------------------- #
# harness: eval + screenshot (curl-drivable, local-only, token-gated — SEC-2)
# --------------------------------------------------------------------------- #
MAX_TIMEOUT = 300.0


def _hostname(value: str | None) -> str:
    """The bare host from a Host header or an Origin URL (no port, no scheme)."""
    text = (value or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if text.startswith("["):                      # [::1]:8766
        return text[1:].split("]", 1)[0]
    if text.count(":") == 1:                      # host:port
        return text.split(":", 1)[0]
    return text


def _is_loopback(host: str | None) -> bool:
    return _hostname(host) in LOOPBACK_HOSTS


def harness_token() -> str:
    """The shared secret for the eval/screenshot bridge.

    ``MEGASPEED_HARNESS_TOKEN`` wins and touches no file; otherwise the token
    lives in ``MEGASPEED_HARNESS_TOKEN_FILE`` (default
    ``<tenants-dir>/.harness-token``) so a local tool can read it without the
    server printing the secret into a log. The file is the source of truth when
    it already holds a token: a second server on the same directory (a
    throwaway one on another port, say) adopts that token instead of
    overwriting it, so it cannot invalidate the live server's token.
    """
    token = (os.environ.get(HARNESS_TOKEN_ENV) or "").strip()
    if token:
        return token
    global _auto_token
    with _auto_token_lock:
        if _auto_token is None:
            _auto_token = _token_for_process()
        return _auto_token


def harness_token_file() -> Path:
    override = (os.environ.get(HARNESS_TOKEN_FILE_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return TENANTS_DIR / ".harness-token"


def _token_for_process() -> str:
    """This process's token: publish a fresh one, or share the file's.

    Create the file exclusively and the first writer wins; anyone who loses the
    race (or starts later against an existing file) uses the token already
    there. Never fail a request over the file: an unusable path just means the
    token stays in memory for this process.
    """
    path = harness_token_file()
    token = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_token_file(path) or token
    except OSError:
        return token
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
    os.chmod(path, 0o600)
    if LOG:
        print(f"megaspeed: harness token written to {path}", file=sys.stderr)
    return token


def _read_token_file(path: Path) -> str | None:
    """The token an existing file holds, if it is safe to adopt it."""
    try:
        # O_NOFOLLOW: never follow a symlink planted in the tenant directory.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return None  # a device/foreign file: not our secret to adopt
        token = handle.read(512).strip()
    return token if token.isascii() and 16 <= len(token) <= 512 else None


def _presented_token(headers, query=None) -> str:
    """The token a client presented: header, bearer auth, or ``?token=``."""
    value = (headers.get(TOKEN_HEADER) or "").strip()
    if value:
        return value
    authorization = (headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if query is not None:
        return str(query.get("token") or "").strip()
    return ""


def _token_ok(presented: str) -> bool:
    expected = harness_token()
    return bool(presented) and presented.isascii() and secrets.compare_digest(presented, expected)


def _direct_loopback(client_host: str | None, host_header: str | None, headers) -> bool:
    """A direct request to a loopback address, not one proxied in from outside."""
    if not _is_loopback(client_host) or not _is_loopback(host_header):
        return False
    return not any((headers.get(name) or "").strip() for name in PROXY_HEADERS)


def _harness_denied(request: Request) -> JSONResponse | None:
    """403 for a non-local caller, 401 for a local caller without the token."""
    client = request.client.host if request.client else ""
    if not _direct_loopback(client, request.headers.get("host"), request.headers):
        return JSONResponse(
            status_code=403,
            content={
                "ok": False,
                "error": "the eval/screenshot bridge is local-only "
                         "(loopback client and Host, no proxy in front)",
                "hint": "run the browser harness against 127.0.0.1, not the public name",
            },
        )
    if not _token_ok(_presented_token(request.headers, request.query_params)):
        return JSONResponse(
            status_code=401,
            content={
                "ok": False,
                "error": "harness token required",
                "hint": f"send {TOKEN_HEADER}: <token> — set {HARNESS_TOKEN_ENV} or read "
                        f"{harness_token_file()}",
            },
        )
    return None


def _origin_allowed(origin: str | None, host: str | None) -> bool:
    """Cross-site websocket protection.

    A browser always sends ``Origin`` on a websocket handshake and a page cannot
    forge it, so ``Origin`` must name the host the socket addressed (same
    origin) or be an explicitly allowed origin. ``MEGASPEED_WS_ORIGINS``
    overrides the default with a comma-separated allowlist (``*`` allows any).
    A non-browser client may omit ``Origin``; it is then a notification-only
    client, never a trusted one.
    """
    if not origin:
        return True
    allowed = [
        entry.strip().rstrip("/")
        for entry in (os.environ.get(HARNESS_ORIGINS_ENV) or "").split(",")
        if entry.strip()
    ]
    if allowed:
        return "*" in allowed or origin.strip().rstrip("/") in allowed
    origin_host = _hostname(origin)
    return bool(origin_host) and (
        origin_host == _hostname(host) or origin_host in LOOPBACK_HOSTS
    )


def _ws_trusted(ws: WebSocket) -> bool:
    """May this websocket receive and answer eval frames?"""
    if _token_ok(_presented_token(ws.headers, ws.query_params)):
        return True
    # The UI page cannot carry a token. A same-origin page served over loopback
    # *is* the browser the harness exists to drive; a page served from a public
    # name is not trusted, so a remote deployment has no bridge at all.
    origin = ws.headers.get("origin")
    client = ws.client.host if ws.client else ""
    host = ws.headers.get("host")
    return bool(origin) and _direct_loopback(client, host, ws.headers) and (
        _hostname(origin) == _hostname(host)
    )



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
    denied = _harness_denied(request)
    if denied is not None:
        return denied
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
    denied = _harness_denied(request)
    if denied is not None:
        return denied
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
    if not _origin_allowed(ws.headers.get("origin"), ws.headers.get("host")):
        # Cross-site websocket hijacking: refuse before accepting (SEC-2).
        await ws.close(code=1008)
        return
    await ws.accept()
    trusted = _ws_trusted(ws)
    await bridge.add(ws, authenticated=trusted)
    try:
        await ws.send_json({
            "event": "hello", "app": "megaspeed",
            "clients": bridge.clients, "harness": trusted,
        })
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
    """Poll every tenant's store; tell connected browsers which ones changed.

    Polling the directory rather than a fixed file is what makes a newly
    imported tenant appear (and a deleted one disappear) without a restart.
    """
    last = _all_stamps()
    while True:
        await asyncio.sleep(1.0)
        stamps = _all_stamps()
        if stamps != last:
            changed = sorted(name for name in stamps if stamps.get(name) != last.get(name))
            removed = sorted(name for name in last if name not in stamps)
            last = stamps
            await bridge.broadcast(
                {"event": "changed", "tenants": changed, "removed": removed}
            )


def _all_stamps() -> dict:
    return {name: _file_stamp(path) for name, path in _tenant_paths().items()}


if APP_DIR.exists():
    app.mount("/", StaticFiles(directory=str(APP_DIR), html=True), name="app")
