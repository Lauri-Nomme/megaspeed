"""A **labels-only** tgz: correct the text labels without re-scraping.

The scrape's text labels are a projection of raw prose (prd/16). The raw prose
lives only in the raw-zone trace (:mod:`ingest.llmtrace`); the bundle carries
the labels. When a better model, a fixed endpoint or a tuned prompt arrives,
re-walking git and the REST API just to re-read the same comments is hours of
work for a 30-token answer.

So :mod:`ingest.relabel` replays the trace through the classifier and writes
this patch: a small tgz that **only** carries label corrections.

    export/hans-labels.tgz
      manifest.json    # kind: "label-patch", source, counts, classifier report
      labels.jsonl     # one row per corrected THREAD / question phase
      sha256sums       # integrity

The row shapes::

    {"target": "thread", "object_id": "THREAD:DEMO/widget#11096:123",
     "labels": {"comment_type": "nit", "thread_outcome": "unresolved",
                "classifier_version": "ollama-v1"},
     "previous": {"comment_type": "suggestion", ...}}

    {"target": "question", "object_id": "THREAD:DEMO/widget#11096:123",
     "labels": {"phase": "review"}, "classifier_version": "ollama-v1"}

Applying it (:func:`apply_patch`) merges those attribute keys into the stored
THREAD objects and into the ``question_raised`` events linked to them. It never
creates an event or an object, never touches timestamps, actors or relations,
and is idempotent: applying the same patch twice changes nothing the second
time. The import side still runs the whole pseudonymisation contract check
afterwards, so a tampered patch cannot bless free text.

What a patch deliberately cannot do: a re-classification that *adds* or
*removes* a ``question_raised`` event changes the log's structure, not a label.
Those flips are counted in the manifest (``question_flips``) and left alone.
"""

from __future__ import annotations

import hashlib
import json
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

from . import taxonomy
from .bundle import BUNDLE_DIR, SUMS, BundleError, _jsonl, _tamperproof_tar

PATCH_KIND = "label-patch"
SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({SCHEMA_VERSION})
LABELS = "labels.jsonl"
MANIFEST = "manifest.json"
MEMBERS = (MANIFEST, LABELS, SUMS)

#: The object ids a label row may name. A THREAD label and a question phase are
#: the only text-label carriers in the store; a PR is allowed for a description
#: question. Keep the shape check identical to ``store.object_shape_problems``.
_THREAD_ID = re.compile(r"THREAD:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+:\d+")
_PR_ID = re.compile(r"PR:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+")
TARGETS = ("thread", "question")
#: The label keys each target may carry.
TARGET_LABELS = {
    "thread": frozenset({"comment_type", "thread_outcome", "classifier_version"}),
    "question": frozenset({"phase"}),
}
#: A question row either **sets** the phase on the event that exists, **adds**
#: the derived event (a re-label turned a non-question into a question) or
#: **deletes** it (the reverse). Add/delete rewrite the derived event, never the
#: source: no occurred activity is touched.
ACTIONS = ("set", "add", "delete")
VOCAB = {
    "comment_type": taxonomy.COMMENT_TYPE,
    "thread_outcome": taxonomy.THREAD_OUTCOME,
    "phase": taxonomy.QUESTION_PHASE,
}


class PatchError(BundleError):
    """The tgz is not a label patch we may apply."""


@dataclass
class LabelPatch:
    path: Path
    manifest: dict
    rows: list[dict] = field(default_factory=list)
    sha256: str = ""

    @property
    def source(self) -> str:
        return str(self.manifest.get("source") or "unknown")

    @property
    def counts(self) -> dict:
        return dict(self.manifest.get("counts") or {})


# --------------------------------------------------------------------------- #
# writing (relabel side, raw zone)
# --------------------------------------------------------------------------- #
def write_patch(path: str | Path, *, manifest: dict, rows: list[dict]) -> dict:
    """Write the tamperproof tgz; return ``{path, bytes, sha256, counts}``."""
    path = Path(path)
    manifest = dict(manifest)
    manifest["kind"] = PATCH_KIND
    manifest.setdefault("schema_version", SCHEMA_VERSION)
    counts = {"rows": len(rows)}
    for target in TARGETS:
        counts[target] = sum(1 for row in rows if row.get("target") == target)
    manifest["counts"] = {**manifest.get("counts", {}), **counts}
    payload = {
        LABELS: _jsonl(rows),
        MANIFEST: (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    }
    payload[SUMS] = "".join(
        f"{hashlib.sha256(payload[name]).hexdigest()}  {name}\n"
        for name in (MANIFEST, LABELS)
    ).encode()
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
def _members(path: Path) -> dict[str, bytes]:
    try:
        with tarfile.open(path, "r:gz") as tar:
            out: dict[str, bytes] = {}
            for member in tar.getmembers():
                if not member.isfile():
                    raise PatchError(f"{path}: member {member.name!r} is not a regular file")
                name = Path(member.name)
                if name.is_absolute() or ".." in name.parts:
                    raise PatchError(f"{path}: unsafe member path {member.name!r}")
                relative = name.relative_to(BUNDLE_DIR) if name.parts[:1] == (BUNDLE_DIR,) else name
                key = relative.as_posix()
                if key not in MEMBERS:
                    raise PatchError(f"{path}: unexpected member {member.name!r}")
                handle = tar.extractfile(member)
                out[key] = handle.read() if handle else b""
    except (tarfile.TarError, OSError) as exc:
        raise PatchError(f"{path}: not a readable tgz ({exc})") from exc
    missing = [name for name in MEMBERS if name not in out]
    if missing:
        raise PatchError(f"{path}: missing member(s) {missing}")
    return out


def _manifest(path: Path, members: dict[str, bytes] | None = None) -> dict:
    """Peek at the manifest, without verifying the rest (the sniff step)."""
    payload = (members or _members(path))[MANIFEST]
    try:
        manifest = json.loads(payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PatchError(f"{path}: manifest.json is not JSON ({exc})") from exc
    if not isinstance(manifest, dict):
        raise PatchError(f"{path}: manifest.json is not a JSON object")
    return manifest


def is_label_patch(path: str | Path) -> bool:
    """``True`` when the tgz says ``kind: label-patch`` (a cheap sniff).

    A corrupt or unreadable file is not a label patch; the ordinary bundle
    reader then reports the real error.
    """
    path = Path(path)
    if not path.is_file():
        return False
    try:
        with tarfile.open(path, "r:gz") as tar:
            member = next(
                (m for m in tar.getmembers()
                 if Path(m.name).as_posix() in (MANIFEST, f"{BUNDLE_DIR}/{MANIFEST}")),
                None,
            )
            if member is None:
                return False
            handle = tar.extractfile(member)
            manifest = json.loads((handle.read() if handle else b"{}").decode())
    except (tarfile.TarError, OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(manifest, dict) and manifest.get("kind") == PATCH_KIND


def _check_sums(path: Path, members: dict[str, bytes]) -> None:
    expected: dict[str, str] = {}
    for line in members[SUMS].decode().splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        if not digest or not name:
            raise PatchError(f"{path}: malformed sha256sums line {line!r}")
        expected[name.strip()] = digest.strip()
    for name in (MANIFEST, LABELS):
        if name not in expected:
            raise PatchError(f"{path}: sha256sums does not cover {name}")
        if hashlib.sha256(members[name]).hexdigest() != expected[name]:
            raise PatchError(f"{path}: checksum mismatch for {name}")
    extra = set(expected) - {MANIFEST, LABELS}
    if extra:
        raise PatchError(f"{path}: sha256sums covers unknown member(s) {sorted(extra)}")


def _event_row(path: Path, object_id: str, event: dict) -> dict:
    """Validate one derived ``question_raised`` event a patch wants to add."""
    from .store import ACTOR_RE, EVENT_KEY_RE, REPO_RE
    from .timeutil import to_naive_utc

    if not isinstance(event, dict):
        raise PatchError(f"{path}: {object_id} event is not an object")
    key = str(event.get("event_key") or "")
    if not re.fullmatch(EVENT_KEY_RE, key):
        raise PatchError(f"{path}: {object_id} event_key {key!r} is not an identifier")
    actor = str(event.get("actor") or "")
    if not re.fullmatch(ACTOR_RE, actor):
        raise PatchError(f"{path}: {object_id} event actor {actor!r} is not a pseudonym")
    repo = str(event.get("repo") or "")
    if not re.fullmatch(REPO_RE, repo):
        raise PatchError(f"{path}: {object_id} event repo {repo!r} is not a slug")
    if not isinstance(event.get("pr_id"), int):
        raise PatchError(f"{path}: {object_id} event pr_id must be an integer")
    try:
        ts = to_naive_utc(event.get("ts"))
        as_of = to_naive_utc(event.get("as_of")) if event.get("as_of") else ts
    except (TypeError, ValueError) as exc:
        raise PatchError(f"{path}: {object_id} event timestamp is bad ({exc})") from exc
    attributes = dict(event.get("attributes") or {})
    try:
        attributes = taxonomy.check_attributes(attributes)
    except taxonomy.TaxonomyError as exc:
        raise PatchError(f"{path}: {object_id} event attributes: {exc}") from exc
    objects = [str(o) for o in event.get("objects") or []]
    if not objects or not any(o == object_id for o in objects):
        raise PatchError(f"{path}: {object_id} event does not reference its object")
    for other in objects:
        if not re.fullmatch(_THREAD_ID.pattern + "|" + _PR_ID.pattern + r"|TICKET:[A-Z][A-Z0-9_]{0,9}-\d{1,6}", other):
            raise PatchError(f"{path}: {object_id} event references a bad object id {other!r}")
    return {
        "event_key": key,
        "ts": ts,
        "as_of": as_of,
        "activity": "question_raised",
        "actor": actor,
        "repo": repo,
        "pr_id": int(event["pr_id"]),
        "objects": objects,
        "attributes": attributes,
        "classifier_version": taxonomy.CLASSIFIER_VERSION,
        "content_hash": None,
    }


def _row(path: Path, row: dict) -> dict:
    target = str(row.get("target") or "")
    if target not in TARGETS:
        raise PatchError(f"{path}: label row target {target!r} is outside {TARGETS}")
    object_id = str(row.get("object_id") or "")
    pattern = _THREAD_ID if target == "thread" else (_THREAD_ID.pattern + "|" + _PR_ID.pattern)
    if not re.fullmatch(pattern, object_id):
        raise PatchError(f"{path}: label row object_id {object_id!r} has an unexpected shape")
    labels = row.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise PatchError(f"{path}: label row {object_id} carries no labels")
    allowed = TARGET_LABELS[target]
    unknown = set(labels) - allowed
    if unknown:
        raise PatchError(
            f"{path}: label row {object_id} sets {sorted(unknown)}, outside {sorted(allowed)}"
        )
    for key, value in labels.items():
        if key == "classifier_version":
            if not re.fullmatch(taxonomy.CLASSIFIER_TOKEN.pattern, str(value)):
                raise PatchError(f"{path}: {object_id} classifier_version {value!r} is not a label")
            continue
        if str(value) not in VOCAB[key]:
            raise PatchError(
                f"{path}: {object_id} {key}={value!r} is outside the closed vocabulary"
            )
    action = str(row.get("action") or "set")
    if action not in ACTIONS:
        raise PatchError(f"{path}: {object_id} action {action!r} is outside {ACTIONS}")
    if target == "thread" and action != "set":
        raise PatchError(f"{path}: {object_id} a thread row has no add/delete action")
    out = {
        "target": target,
        "object_id": object_id,
        "action": action,
        "labels": {str(k): str(v) for k, v in labels.items()},
        "previous": dict(row.get("previous") or {}),
    }
    if action == "add":
        events = row.get("events")
        if not isinstance(events, list) or not events:
            raise PatchError(f"{path}: {object_id} add row carries no derived event(s)")
        out["events"] = [_event_row(path, object_id, event) for event in events]
    return out


def read_patch(path: str | Path) -> LabelPatch:
    """Read + validate a label patch. Raises :class:`PatchError`; never returns junk."""
    path = Path(path)
    if not path.is_file():
        raise PatchError(f"no label patch at {path}")
    members = _members(path)
    _check_sums(path, members)
    manifest = _manifest(path, members)
    if manifest.get("kind") != PATCH_KIND:
        raise PatchError(f"{path}: kind {manifest.get('kind')!r} is not {PATCH_KIND!r}")
    if manifest.get("schema_version") not in SUPPORTED_SCHEMA_VERSIONS:
        raise PatchError(
            f"{path}: schema_version {manifest.get('schema_version')!r} not supported"
            f" (this build reads {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    if not manifest.get("source"):
        raise PatchError(f"{path}: manifest has no source")
    if manifest.get("classifier_version") not in taxonomy.CLASSIFIER_VERSIONS:
        raise PatchError(
            f"{path}: classifier_version {manifest.get('classifier_version')!r} is not known"
        )
    rows: list[dict] = []
    for number, line in enumerate(members[LABELS].decode().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PatchError(f"{path}: labels.jsonl line {number} is not JSON ({exc})") from exc
        if not isinstance(raw, dict):
            raise PatchError(f"{path}: labels.jsonl line {number} is not an object")
        rows.append(_row(path, raw))
    stated = (manifest.get("counts") or {}).get("rows")
    if stated is not None and int(stated) != len(rows):
        raise PatchError(
            f"{path}: manifest says {stated} row(s), the patch holds {len(rows)}"
        )
    return LabelPatch(
        path=path,
        manifest=manifest,
        rows=rows,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


# --------------------------------------------------------------------------- #
# applying (import side; called inside the importer's transaction)
# --------------------------------------------------------------------------- #
def _compact(value: dict) -> str:
    """The store's attribute encoding: compact JSON (the contract scan rejects
    whitespace inside an attribute bag, and ``json.dumps`` pads by default)."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def apply_patch(con, patch: LabelPatch) -> dict:
    """Merge the patch's labels into THREAD objects and derived question events.

    ``set`` updates the phase of the event that exists; ``add`` inserts the
    derived ``question_raised`` (+ a resolution ``question_answered`` when the
    thread was resolved by someone else); ``delete`` removes the derived events
    linked to the object. All three are idempotent and touch no *occurred*
    event: the source activity is never rewritten.

    Threads are merged in **bulk** (one ``SELECT ... IN`` plus one
    ``UPDATE ... FROM``): a 13k-thread corpus is a second, not a minute.
    """
    stats = {
        "threads": 0,
        "threads_missing": 0,
        "threads_unchanged": 0,
        "questions": 0,
        "questions_missing": 0,
        "questions_added": 0,
        "questions_removed": 0,
        "questions_unchanged": 0,
    }
    thread_rows = [row for row in patch.rows if row["target"] == "thread"]
    if thread_rows:
        existing: dict[str, dict] = {}
        ids = [row["object_id"] for row in thread_rows]
        for start in range(0, len(ids), 5000):
            chunk = ids[start : start + 5000]
            placeholders = ",".join("?" * len(chunk))
            for object_id, attributes in con.execute(
                "SELECT object_id, CAST(attributes AS VARCHAR) FROM objects"
                f" WHERE object_type = 'THREAD' AND object_id IN ({placeholders})",
                chunk,
            ).fetchall():
                existing[object_id] = json.loads(attributes or "{}")
        changes: list[tuple[str, str]] = []
        for row in thread_rows:
            attributes = existing.get(row["object_id"])
            if attributes is None:
                stats["threads_missing"] += 1
                continue
            merged = {**attributes, **row["labels"]}
            if merged == attributes:
                stats["threads_unchanged"] += 1
                continue
            changes.append((row["object_id"], _compact(merged)))
        if changes:
            merged_rel = con.values(*changes)
            con.execute(
                "UPDATE objects SET attributes = CAST(m.col1 AS JSON)"
                " FROM merged_rel m"
                " WHERE objects.object_type = 'THREAD' AND objects.object_id = m.col0"
            )
            stats["threads"] = len(changes)

    for row in patch.rows:
        if row["target"] != "question":
            continue
        action = row.get("action") or "set"
        if action == "add":
            for event in row.get("events") or []:
                if con.execute(
                    "SELECT 1 FROM events WHERE event_key = ?", [event["event_key"]]
                ).fetchone():
                    stats["questions_unchanged"] += 1
                    continue
                con.execute(
                    "INSERT INTO events (event_key, ts, activity, actor, repo, pr_id,"
                    " attributes, classifier_version, content_hash, as_of)"
                    " VALUES (?, ?, ?, ?, ?, ?, CAST(? AS JSON), ?, ?, ?)",
                    [
                        event["event_key"], event["ts"], event["activity"],
                        event["actor"], event["repo"], event["pr_id"],
                        _compact(event["attributes"]), event["classifier_version"],
                        event["content_hash"], event["as_of"],
                    ],
                )
                for object_id in event["objects"]:
                    con.execute(
                        "INSERT OR IGNORE INTO event_objects"
                        " (event_key, object_type, object_id) VALUES (?, ?, ?)",
                        [event["event_key"], object_id.split(":", 1)[0], object_id],
                    )
                stats["questions_added"] += 1
            continue
        if action == "delete":
            events = con.execute(
                "SELECT e.event_key FROM events e"
                " JOIN event_objects r ON e.event_key = r.event_key"
                " WHERE e.activity IN ('question_raised', 'question_answered')"
                "   AND r.object_type = ? AND r.object_id = ?",
                [row["object_id"].split(":", 1)[0], row["object_id"]],
            ).fetchall()
            keys = [str(event[0]) for event in events]
            # A PR also relates to its threads' questions; only the description
            # event (and nothing answered) is the PR's own.
            if row["object_id"].startswith("PR:"):
                keys = [key for key in keys if key.endswith(":desc:q")]
            if not keys:
                stats["questions_unchanged"] += 1
                continue
            placeholders = ",".join("?" * len(keys))
            con.execute(f"DELETE FROM event_objects WHERE event_key IN ({placeholders})", keys)
            con.execute(f"DELETE FROM events WHERE event_key IN ({placeholders})", keys)
            stats["questions_removed"] += len(keys)
            continue
        events = con.execute(
            "SELECT e.event_key, CAST(e.attributes AS VARCHAR)"
            " FROM events e JOIN event_objects r ON e.event_key = r.event_key"
            " WHERE e.activity = 'question_raised'"
            "   AND r.object_type = ? AND r.object_id = ?",
            [row["object_id"].split(":", 1)[0], row["object_id"]],
        ).fetchall()
        # A PR also relates to its threads' questions; only the description
        # event (`…:desc:q`) is the PR's own phase. Same rule as relabel.
        if row["object_id"].startswith("PR:"):
            events = [event for event in events if str(event[0]).endswith(":desc:q")]
        if not events:
            stats["questions_missing"] += 1
            continue
        for event_key, attributes in events:
            current = json.loads(attributes or "{}")
            merged = {**current, **row["labels"]}
            if merged == current:
                continue
            con.execute(
                "UPDATE events SET attributes = CAST(? AS JSON) WHERE event_key = ?",
                [_compact(merged), event_key],
            )
            stats["questions"] += 1
    return stats


def summary(patch: LabelPatch) -> dict:
    """A one-screen description for the CLI (no raw content anywhere)."""
    counts = patch.counts
    return {
        "kind": PATCH_KIND,
        "bundle": str(patch.path),
        "sha256": patch.sha256,
        "source": patch.source,
        "created_at": patch.manifest.get("created_at"),
        "applies_to": patch.manifest.get("applies_to"),
        "classifier": patch.manifest.get("text_classifier") or {},
        "rows": len(patch.rows),
        "counts": counts,
        "question_flips": counts.get("question_flips", 0),
    }


__all__ = [
    "LabelPatch",
    "PATCH_KIND",
    "PatchError",
    "SCHEMA_VERSION",
    "apply_patch",
    "is_label_patch",
    "read_patch",
    "summary",
    "write_patch",
]
