"""Tenants = parallel datasets, one DuckDB store each (prd/12).

A tenant is a file: ``<tenants-dir>/<tenant>.duckdb``. The registry **is** the
directory (prd/12 decision 2) rather than a ``tenants.json`` that can drift —
import a store into ``data/`` and it appears in the switcher; delete it and it
is gone. Nothing here opens a store: this module is only path logic, so it
stays importable and testable without DuckDB. ``server.app`` owns connections,
retries and the error shapes.

A tenant name doubles as a file name, so it must be a single safe path segment:
no ``/``, no ``..``, no leading dot. Anything else in the directory is not a
tenant and is never reachable through ``?tenant=``.
"""

from __future__ import annotations

import re
from pathlib import Path

STORE_SUFFIX = ".duckdb"

# One safe path segment: starts alphanumeric, then letters/digits/._- (≤64).
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def valid_name(name: str) -> bool:
    """Is ``name`` a legal tenant name (i.e. a safe single path segment)?"""
    return bool(NAME_RE.match(str(name or "")))


def discover(directory: str | Path) -> dict[str, Path]:
    """Every tenant store in ``directory``, sorted by name.

    Only regular ``*.duckdb`` files with a legal stem count. ``*.duckdb.wal``
    (an ingest run in flight) and stray files are not tenants.
    """
    try:
        entries = sorted(Path(directory).glob(f"*{STORE_SUFFIX}"), key=lambda p: p.name)
    except OSError:
        return {}
    found: dict[str, Path] = {}
    for path in entries:
        if not path.is_file():
            continue
        if not valid_name(path.stem):
            continue
        found[path.stem] = path
    return found


def default_name(paths: dict[str, Path], preferred: str | None = None) -> str | None:
    """The tenant served when a request does not name one.

    ``preferred`` wins if it is a real tenant (an explicit ``MEGASPEED_TENANT``
    or the store ``MEGASPEED_DB`` points at); otherwise the first by name, so
    the choice is deterministic and the UI can default to it too.
    """
    if not paths:
        return None
    if preferred and preferred in paths:
        return preferred
    return next(iter(paths))
