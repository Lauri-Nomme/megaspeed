"""The transfer contract: a tgz of **already-pseudonymised** rows (prd/11 §2).

This module is the airgap. It is the only thing that crosses from the scraper
(next to the source) to the main side, and it is deliberately dumb: write four
files into a tar, read them back, refuse anything that does not check out.

```
export/
  manifest.json      # schema_version, source(s), watermark_ts, classifier_version,
                     #   projects/repos, counts, created_at
  events.jsonl       # pseudonymised events -- one JSON object per line
  objects.jsonl      # PR / COMMIT / PRREV / THREAD / TICKET / PERSON objects
  sha256sums         # integrity
```

Rules that are enforced here, not by convention:

* **No identity map.** The raw -> pseudonym table never enters the bundle; the
  bundle is pseudonymised *by construction* (prd/11 §5).
* **Same row shapes as the store** (``event_key``, ``ts``, ``activity``,
  ``actor``, ``objects``, ``attributes``, ...) so import is a straight load.
* **Integrity.** ``sha256sums`` covers every other member; import verifies it
  before reading a single row.
* **Stdlib only.** Both sides import this file; the scraper artifact must not
  need DuckDB to write a bundle.

The reader is a *validator*, not a parser with a fallback: an unknown
``schema_version``, an unknown ``classifier_version``, a missing member, a bad
checksum or a count mismatch raises :class:`BundleError` and nothing is merged
(prd/11 §6).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import taxonomy
from .timeutil import iso, to_naive_utc

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
# The tar/ directory every member lives under. Fixed on purpose: a bundle is
# read by a machine that only knows this contract.
BUNDLE_DIR = "export"
MANIFEST = "manifest.json"
EVENTS = "events.jsonl"
OBJECTS = "objects.jsonl"
SUMS = "sha256sums"
MEMBERS = (MANIFEST, EVENTS, OBJECTS, SUMS)


class BundleError(RuntimeError):
    """The tgz is not a bundle we may import (bad integrity, schema or shape)."""


@dataclass
class Bundle:
    path: Path
    manifest: dict
    events: list[dict] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    relations: list[tuple[str, str, str]] = field(default_factory=list)
    sha256: str = ""

    @property
    def source(self) -> str:
        return str(self.manifest.get("source") or "unknown")

    @property
    def watermark(self) -> datetime | None:
        value = self.manifest.get("watermark_ts")
        return to_naive_utc(value) if value else None

    @property
    def classifier_version(self) -> str:
        return str(self.manifest.get("classifier_version") or "")

    @property
    def ticket_projects(self) -> tuple[str, ...]:
        return tuple(str(p).upper() for p in self.manifest.get("ticket_projects") or ())


# --------------------------------------------------------------------------- #
# writing (scrape side)
# --------------------------------------------------------------------------- #
def _jsonl(rows: list[dict]) -> bytes:
    buffer = io.StringIO()
    for row in rows:
        buffer.write(json.dumps(row, sort_keys=True, separators=(",", ":"), default=str))
        buffer.write("\n")
    return buffer.getvalue().encode()


def serialize_event(event: dict, object_ids: list[str]) -> dict:
    """One in-memory event -> one ``events.jsonl`` row (store shape + objects)."""
    return {
        "event_key": event["event_key"],
        "ts": iso(event["ts"]),
        "activity": event["activity"],
        "actor": event["actor"],
        "repo": event.get("repo"),
        "pr_id": event.get("pr_id"),
        "objects": list(object_ids),
        "attributes": event.get("attributes") or {},
        "classifier_version": event["classifier_version"],
        "content_hash": event.get("content_hash"),
        "as_of": iso(event.get("as_of")) if event.get("as_of") else None,
    }


def serialize_object(record: dict) -> dict:
    """One in-memory object row -> one ``objects.jsonl`` row."""
    return {
        "object_type": record["object_type"],
        "object_id": record["object_id"],
        "repo": record.get("repo"),
        "pr_id": record.get("pr_id"),
        "ticket_keys": list(record.get("ticket_keys") or []),
        "actor": record.get("actor"),
        "state": record.get("state"),
        "created_at": iso(record.get("created_at")) if record.get("created_at") else None,
        "closed_at": iso(record.get("closed_at")) if record.get("closed_at") else None,
        "head_commit": record.get("head_commit"),
        "message_class": record.get("message_class"),
        "attributes": record.get("attributes") or {},
    }


def _tamperproof_tar(members: list[tuple[str, bytes]]) -> bytes:
    """Deterministic tar.gz: fixed order, mtime, uid/gid and modes.

    Same input -> same bytes, so a bundle's sha256 is a real provenance handle
    (and CI can compare rebuilds).
    """
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name, payload in members:
                info = tarfile.TarInfo(name=f"{BUNDLE_DIR}/{name}")
                info.size = len(payload)
                info.mtime = 0
                info.mode = 0o644
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(payload))
    return raw.getvalue()


def write_bundle(
    path: str | Path,
    *,
    manifest: dict,
    events: list[dict],
    objects: list[dict],
    relations: list[tuple[str, str, str]] | None = None,
) -> dict:
    """Write the tgz; return ``{path, bytes, sha256, counts}``.

    ``events`` are in-memory reducer rows (``ts``/``as_of`` as datetimes);
    object ids per event come from ``relations`` unless a row already carries an
    ``objects`` list.
    """
    path = Path(path)
    by_event: dict[str, list[str]] = {}
    for event_key, _object_type, object_id in relations or ():
        by_event.setdefault(event_key, []).append(object_id)

    event_rows = []
    for event in events:
        object_ids = event.get("objects") or by_event.get(event["event_key"], [])
        event_rows.append(serialize_event(event, object_ids))
    object_rows = [serialize_object(row) for row in objects]

    payload = {
        EVENTS: _jsonl(event_rows),
        OBJECTS: _jsonl(object_rows),
    }
    manifest = dict(manifest)
    manifest.setdefault("schema_version", SCHEMA_VERSION)
    manifest.setdefault("counts", {})
    manifest["counts"] = {
        **manifest["counts"],
        "events": len(event_rows),
        "objects": len(object_rows),
    }
    payload[MANIFEST] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()

    sums = "".join(
        f"{hashlib.sha256(payload[name]).hexdigest()}  {name}\n"
        for name in (MANIFEST, EVENTS, OBJECTS)
    ).encode()
    payload[SUMS] = sums

    blob = _tamperproof_tar([(name, payload[name]) for name in MEMBERS])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return {
        "path": str(path),
        "bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "counts": manifest["counts"],
    }


# --------------------------------------------------------------------------- #
# reading (import side)
# --------------------------------------------------------------------------- #
def _read_members(path: Path) -> dict[str, bytes]:
    try:
        with tarfile.open(path, "r:gz") as tar:
            out: dict[str, bytes] = {}
            for member in tar.getmembers():
                if not member.isfile():
                    raise BundleError(f"{path}: member {member.name!r} is not a regular file")
                name = Path(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise BundleError(f"{path}: unsafe member path {member.name!r}")
                relative = name.relative_to(BUNDLE_DIR) if name.parts[:1] == (BUNDLE_DIR,) else name
                key = relative.as_posix()
                if key not in MEMBERS:
                    # Unknown extra members are refused: a bundle is a contract,
                    # not a dumping ground.
                    raise BundleError(f"{path}: unexpected member {member.name!r}")
                handle = tar.extractfile(member)
                out[key] = handle.read() if handle else b""
    except (tarfile.TarError, OSError) as exc:
        raise BundleError(f"{path}: not a readable tgz ({exc})") from exc
    missing = [name for name in MEMBERS if name not in out]
    if missing:
        raise BundleError(f"{path}: missing member(s) {missing}")
    return out


def _verify_sums(path: Path, members: dict[str, bytes]) -> None:
    expected: dict[str, str] = {}
    for line in members[SUMS].decode().splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        if not digest or not name:
            raise BundleError(f"{path}: malformed sha256sums line {line!r}")
        expected[name.strip()] = digest.strip()
    for name in (MANIFEST, EVENTS, OBJECTS):
        if name not in expected:
            raise BundleError(f"{path}: sha256sums does not cover {name}")
        actual = hashlib.sha256(members[name]).hexdigest()
        if actual != expected[name]:
            raise BundleError(f"{path}: checksum mismatch for {name}")
    extra = set(expected) - {MANIFEST, EVENTS, OBJECTS}
    if extra:
        raise BundleError(f"{path}: sha256sums covers unknown member(s) {sorted(extra)}")


def _jsonl_rows(path: Path, name: str, payload: bytes) -> list[dict]:
    rows: list[dict] = []
    for number, line in enumerate(payload.decode().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BundleError(f"{path}: {name} line {number} is not JSON ({exc})") from exc
        if not isinstance(row, dict):
            raise BundleError(f"{path}: {name} line {number} is not a JSON object")
        rows.append(row)
    return rows


def _event_row(path: Path, row: dict) -> dict:
    for required in ("event_key", "ts", "activity", "actor"):
        if not row.get(required):
            raise BundleError(f"{path}: event without {required!r}: {row.get('event_key')!r}")
    try:
        ts = to_naive_utc(row["ts"])
        as_of = to_naive_utc(row["as_of"]) if row.get("as_of") else ts
    except ValueError as exc:
        raise BundleError(f"{path}: event {row['event_key']!r} has a bad timestamp ({exc})") from exc
    if row["activity"] not in taxonomy.ACTIVITIES:
        raise BundleError(
            f"{path}: event {row['event_key']!r} activity {row['activity']!r} is outside the vocabulary"
        )
    objects = row.get("objects") or []
    if not isinstance(objects, list) or not objects:
        raise BundleError(f"{path}: event {row['event_key']!r} references no object")
    return {
        "event_key": str(row["event_key"]),
        "ts": ts,
        "activity": str(row["activity"]),
        "actor": str(row["actor"]),
        "repo": row.get("repo"),
        "pr_id": row.get("pr_id"),
        "objects": [str(o) for o in objects],
        "attributes": dict(row.get("attributes") or {}),
        "classifier_version": str(row.get("classifier_version") or ""),
        "content_hash": row.get("content_hash"),
        "as_of": as_of,
    }


def _object_row(path: Path, row: dict) -> dict:
    if not row.get("object_type") or not row.get("object_id"):
        raise BundleError(f"{path}: object row without type/id: {row!r}")
    if row["object_type"] not in taxonomy.OBJECT_TYPES:
        raise BundleError(
            f"{path}: object {row['object_id']!r} type {row['object_type']!r} is outside the vocabulary"
        )
    return {
        "object_type": str(row["object_type"]),
        "object_id": str(row["object_id"]),
        "repo": row.get("repo"),
        "pr_id": row.get("pr_id"),
        "ticket_keys": [str(k) for k in row.get("ticket_keys") or []],
        "actor": row.get("actor"),
        "state": row.get("state"),
        "created_at": to_naive_utc(row["created_at"]) if row.get("created_at") else None,
        "closed_at": to_naive_utc(row["closed_at"]) if row.get("closed_at") else None,
        "head_commit": row.get("head_commit"),
        "message_class": row.get("message_class"),
        "attributes": dict(row.get("attributes") or {}),
    }


def read_bundle(path: str | Path) -> Bundle:
    """Read + validate a tgz. Raises :class:`BundleError`; never returns junk."""
    path = Path(path)
    if not path.is_file():
        raise BundleError(f"no bundle at {path}")
    members = _read_members(path)
    _verify_sums(path, members)

    try:
        manifest: dict[str, Any] = json.loads(members[MANIFEST].decode())
    except json.JSONDecodeError as exc:
        raise BundleError(f"{path}: manifest.json is not JSON ({exc})") from exc
    if not isinstance(manifest, dict):
        raise BundleError(f"{path}: manifest.json is not a JSON object")

    version = manifest.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise BundleError(
            f"{path}: schema_version {version!r} not supported"
            f" (this build reads {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    classifier = manifest.get("classifier_version")
    if classifier not in taxonomy.CLASSIFIER_VERSIONS:
        raise BundleError(
            f"{path}: classifier_version {classifier!r} is not known to this build"
            f" ({sorted(taxonomy.CLASSIFIER_VERSIONS)})"
        )
    if not manifest.get("source"):
        raise BundleError(f"{path}: manifest has no source")

    events = [_event_row(path, row) for row in _jsonl_rows(path, EVENTS, members[EVENTS])]
    objects = [_object_row(path, row) for row in _jsonl_rows(path, OBJECTS, members[OBJECTS])]

    counts = manifest.get("counts") or {}
    if counts.get("events") is not None and int(counts["events"]) != len(events):
        raise BundleError(
            f"{path}: manifest says {counts['events']} event(s), the bundle holds {len(events)}"
        )
    if counts.get("objects") is not None and int(counts["objects"]) != len(objects):
        raise BundleError(
            f"{path}: manifest says {counts['objects']} object(s), the bundle holds {len(objects)}"
        )

    relations = [
        (event["event_key"], object_id.split(":", 1)[0], object_id)
        for event in events
        for object_id in event["objects"]
    ]
    return Bundle(
        path=path,
        manifest=manifest,
        events=events,
        objects=objects,
        relations=relations,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
