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
import os
import re
import shutil
import tarfile
import tempfile
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
#: Suffix of the in-flight staging directory a live scrape checkpoints into
#: (``export/del.tgz.parts/``). Local state, never transferred: the artifact is
#: still the single tgz written at the end of the run.
SPOOL_SUFFIX = ".parts"
#: Part file names inside the spool: ``part-00001.events.jsonl`` and
#: ``part-00001.objects.jsonl``. Sorted by name = written order.
SPOOL_EVENTS = "part-{index:05d}.events.jsonl"
SPOOL_OBJECTS = "part-{index:05d}.objects.jsonl"
#: Matches either part name, so a resumed spool can continue its numbering.
_PART_NAME = re.compile(r"part-(?P<index>\d+)\.(?:events|objects)\.jsonl")


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

    @property
    def ticket_keys(self) -> tuple[str, ...]:
        """The explicit key set this bundle was scraped with (prd/18), if any."""
        return tuple(str(k).strip().upper() for k in self.manifest.get("ticket_keys") or ())


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


def rows_for_events(
    events: list[dict], relations: list[tuple[str, str, str]] | None = None
) -> list[dict]:
    """In-memory reducer events -> ``events.jsonl`` rows (``objects`` resolved).

    Object ids per event come from ``relations`` unless a row already carries an
    ``objects`` list (a row read back from a bundle/spool does).
    """
    by_event: dict[str, list[str]] = {}
    for event_key, _object_type, object_id in relations or ():
        by_event.setdefault(event_key, []).append(object_id)
    return [
        serialize_event(event, event.get("objects") or by_event.get(event["event_key"], []))
        for event in events
    ]


def rows_for_objects(objects: list[dict]) -> list[dict]:
    """In-memory object records -> ``objects.jsonl`` rows."""
    return [serialize_object(row) for row in objects]


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
    event_rows = rows_for_events(events, relations)
    object_rows = rows_for_objects(objects)

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
# in-flight staging (scrape side): incremental checkpoints
# --------------------------------------------------------------------------- #
def spool_dir(path: str | Path) -> Path:
    """``export/del.tgz`` -> ``export/del.tgz.parts/`` (in-flight rows, local)."""
    path = Path(path)
    return path.parent / f"{path.name}{SPOOL_SUFFIX}"


class BundleSpool:
    """Append-only staging area for an **in-flight** scrape (crash safety).

    A scrape checkpoints every ``--checkpoint-every`` PRs so a crash costs the
    PRs since the last checkpoint, not the whole run. Re-serialising the whole
    tgz at every checkpoint is O(N) *per checkpoint* -- O(N²) per run, 45 s of
    pure gzip+tar at 4,000 PRs -- so a checkpoint instead stages **only the rows
    that are new since the last one** into
    ``<out>.parts/part-NNNNN.{events,objects}.jsonl`` and the tgz is written
    once, at the end of the run (:func:`write_bundle`).

    The parts hold exactly the rows a bundle would (:func:`serialize_event` /
    :func:`serialize_object`), so :func:`read_spool` validates them with the
    same reader the import side uses: a crash mid-run leaves a *readable*
    in-flight artifact and :func:`read_inflight` hands it to the resume path.

    Row **identity**, not row content, decides what is new: the reducer appends
    rows and never mutates one in place, so ``is`` is the whole test -- a
    checkpoint costs one dict lookup per row plus the serialisation of the delta
    instead of a full re-serialisation. :meth:`append` returns the bytes it
    wrote, which is what the "a checkpoint is O(delta)" regression test pins.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        # A resumed run continues *after* the parts a dead run left behind: a
        # fresh part index would overwrite them (and lose their rows).
        self.parts = self._existing_parts()
        self.bytes_written = 0
        self.events_written = 0
        self.objects_written = 0
        self._events: dict[str, dict] = {}
        self._objects: dict[str, dict] = {}
        self._manifest: dict = {}

    # -- lifecycle -------------------------------------------------------
    def reset(self) -> None:
        """Start a run from nothing (discard a stale spool from a dead run)."""
        shutil.rmtree(self.directory, ignore_errors=True)
        self.parts = 0
        self.bytes_written = 0
        self.events_written = 0
        self.objects_written = 0
        self._events = {}
        self._objects = {}
        self._manifest = {}

    def resume(self, carried: "Bundle") -> None:
        """Adopt the rows of the interrupted run this one is resuming.

        The carried rows are the *same dict objects* the caller then passes back
        into :meth:`append` (the resume merge keeps references), so indexing them
        here is enough to stop them being staged a second time. If the spool is
        a different (or empty) directory -- a resume into another ``--out`` --
        they are staged once, because this spool does not hold them yet.
        """
        self._events = {str(row["event_key"]): row for row in carried.events}
        self._objects = {str(row["object_id"]): row for row in carried.objects}
        self._manifest = dict(carried.manifest)
        if not self._has_parts():
            self._write(
                list(self._events.values()),
                list(self._objects.values()),
                [],
                manifest=self._manifest,
            )

    def finish(self) -> None:
        """The final tgz landed: the in-flight rows are redundant, drop them."""
        shutil.rmtree(self.directory, ignore_errors=True)

    @property
    def exists(self) -> bool:
        return self._has_parts() or (self.directory / MANIFEST).is_file()

    def _existing_parts(self) -> int:
        """The highest part index already on disk (0 when the spool is new)."""
        if not self.directory.is_dir():
            return 0
        highest = 0
        for part in self.directory.iterdir():
            match = _PART_NAME.fullmatch(part.name)
            if match:
                highest = max(highest, int(match["index"]))
        return highest

    def _has_parts(self) -> bool:
        return self.directory.is_dir() and any(
            self.directory.glob(f"part-*.{EVENTS}")
        )

    # -- staging ---------------------------------------------------------
    def append(
        self,
        *,
        manifest: dict,
        events: list[dict],
        objects: list[dict],
        relations: list[tuple[str, str, str]] | None = None,
    ) -> dict:
        """Stage the rows that are new since the last checkpoint.

        Returns ``{bytes, manifest_bytes, events, objects, parts, total_bytes}``
        for that one checkpoint (``bytes`` is the rows of the delta, never the
        whole prefix).
        """
        fresh_events = [
            row for row in events if self._events.get(str(row["event_key"])) is not row
        ]
        # Objects are deduped by identity *or* equality: the PERSON rows are
        # re-derived from the actor set on every refresh (a fresh dict with the
        # same content), and re-staging them at every checkpoint would put the
        # O(prefix) back into the object half.
        fresh_objects = [
            row
            for row in objects
            if (known := self._objects.get(str(row["object_id"]))) is not row
            and known != row
        ]
        manifest = dict(manifest)
        manifest.setdefault("schema_version", SCHEMA_VERSION)
        manifest.setdefault("counts", {})
        manifest["counts"] = {
            **manifest["counts"],
            "events": len(events),
            "objects": len(objects),
        }
        if not fresh_events and not fresh_objects and manifest == self._manifest:
            return {
                "bytes": 0,
                "manifest_bytes": 0,
                "events": 0,
                "objects": 0,
                "parts": self.parts,
                "total_bytes": self.bytes_written,
            }
        written = self._write(fresh_events, fresh_objects, relations, manifest=manifest)
        for row in fresh_events:
            self._events[str(row["event_key"])] = row
        for row in fresh_objects:
            self._objects[str(row["object_id"])] = row
        return written

    def _write(
        self,
        events: list[dict],
        objects: list[dict],
        relations: list[tuple[str, str, str]] | None,
        *,
        manifest: dict,
    ) -> dict:
        self.parts += 1
        index = self.parts
        row_bytes = 0
        event_rows = rows_for_events(events, relations)
        object_rows = rows_for_objects(objects)
        if event_rows:
            row_bytes += self._part(SPOOL_EVENTS.format(index=index), event_rows)
        if object_rows:
            row_bytes += self._part(SPOOL_OBJECTS.format(index=index), object_rows)
        # The manifest goes last: the parts it describes are already durable, so
        # a crash between the two leaves a readable (if slightly older) spool.
        manifest_bytes = self._manifest_file(manifest)
        self.bytes_written += row_bytes + manifest_bytes
        self.events_written += len(event_rows)
        self.objects_written += len(object_rows)
        return {
            # ``bytes`` is the *rows* of this checkpoint (the O(delta) figure the
            # regression test pins); the manifest is a small fixed cost.
            "bytes": row_bytes,
            "manifest_bytes": manifest_bytes,
            "events": len(event_rows),
            "objects": len(object_rows),
            "parts": self.parts,
            "total_bytes": self.bytes_written,
        }

    def _part(self, name: str, rows: list[dict]) -> int:
        payload = _jsonl(rows)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / name).write_bytes(payload)
        return len(payload)

    def _manifest_file(self, manifest: dict) -> int:
        """Write ``manifest.json`` through a temp file + rename (never torn)."""
        payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        self.directory.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            dir=self.directory, prefix=".manifest.", suffix=".part"
        )
        os.close(handle)
        temp = Path(temp_name)
        try:
            temp.write_bytes(payload)
            os.replace(temp, self.directory / MANIFEST)
        finally:
            temp.unlink(missing_ok=True)
        self._manifest = manifest
        return len(payload)


def read_spool(directory: str | Path) -> Bundle:
    """Read + validate an in-flight spool as if it were a bundle.

    Every row goes through the same validators :func:`read_bundle` uses, so a
    resumed run cannot be handed junk. The manifest's ``counts`` are *not*
    enforced: the manifest is written after the parts it describes, so a crash
    in that window legitimately leaves more rows on disk than the last manifest
    counted.
    """
    directory = Path(directory)
    manifest_path = directory / MANIFEST
    if not manifest_path.is_file():
        raise BundleError(f"no spool at {directory}")
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise BundleError(f"{directory}: manifest.json is not JSON ({exc})") from exc
    if not isinstance(manifest, dict):
        raise BundleError(f"{directory}: manifest.json is not a JSON object")
    _check_manifest(directory, manifest)

    events: dict[str, dict] = {}
    objects: dict[str, dict] = {}
    for part in sorted(directory.glob(f"part-*.{EVENTS}")):
        try:
            payload = part.read_bytes()
        except OSError as exc:
            raise BundleError(f"{part}: unreadable spool part ({exc})") from exc
        for row in _jsonl_rows(part, EVENTS, payload):
            parsed = _event_row(part, row)
            events[str(parsed["event_key"])] = parsed
    for part in sorted(directory.glob(f"part-*.{OBJECTS}")):
        try:
            payload = part.read_bytes()
        except OSError as exc:
            raise BundleError(f"{part}: unreadable spool part ({exc})") from exc
        for row in _jsonl_rows(part, OBJECTS, payload):
            parsed = _object_row(part, row)
            objects[str(parsed["object_id"])] = parsed

    event_list = list(events.values())
    object_list = list(objects.values())
    relations = [
        (event["event_key"], object_id.split(":", 1)[0], object_id)
        for event in event_list
        for object_id in event["objects"]
    ]
    return Bundle(
        path=directory,
        manifest=manifest,
        events=event_list,
        objects=object_list,
        relations=relations,
    )


def inflight_path(path: str | Path) -> Path | None:
    """Where the readable artifact of an interrupted run at ``path`` lives.

    The incremental spool (this build's crash-safety mechanism) wins over a
    partial tgz written by an older build, so a state directory from a previous
    version still resumes.
    """
    spool = spool_dir(path)
    if (spool / MANIFEST).is_file():
        return spool
    candidate = Path(path)
    return candidate if candidate.is_file() else None


def read_inflight(path: str | Path) -> Bundle:
    """Read the in-flight artifact of an interrupted run at ``path``."""
    found = inflight_path(path)
    if found is None:
        raise BundleError(f"no in-flight checkpoint at {path}")
    if found.is_dir():
        return read_spool(found)
    return read_bundle(found)


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


def _check_manifest(path: Path, manifest: dict) -> None:
    """The schema/classifier/source gates every readable artifact must pass."""
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
    _check_manifest(path, manifest)

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
