"""Identity maintenance: one auditable ``rekey`` command (prd/71).

Two identity repairs were done by hand in SQL on 2026-11-23: surname-first
Bitbucket pseudonyms were swapped (``NoLa`` -> ``LaNo``) and every numeric-suffix
variant was merged into its base (``MaPa2`` -> ``MaPa``). The ad-hoc scripts were
not tested, not idempotent, and never touched the raw-zone identity map or a
label patch. This module is that work, once:

    python3 -m ingest rekey --db data/demo.duckdb \\
        --identity export/demo.state/identity.json \\
        [--map OLD=NEW ...] [--merge-suffixes] \\
        [--patch export/labels.tgz --out export/labels.rekeyed.tgz] \\
        [--dry-run | --apply] [--no-backup]

The **store**, the **identity map** and a **label patch** are repaired together:

* ``event_objects`` PERSON links move from ``PERSON:OLD`` to ``PERSON:NEW``
  (the colliding row is dropped *before* the update -- the natural key is
  ``(event_key, object_type, object_id)``, so an update into an existing row
  would violate it);
* ``events.actor`` and ``objects.actor`` are renamed;
* the PERSON row is renamed (its ``attributes.actor_kind`` kept) or merged, and
  a store-wide sweep removes only PERSON rows whose pseudonym is referenced
  nowhere -- in the event log, the object actor column, or a link. A PERSON row
  with *no event link* is still the roster (the fixture store has two), so
  "orphan" here means "has no reference at all", never "has no link";
* the identity map's stored ``pseudonym`` values are updated (raw zone, never
  served) and duplicate targets are reported instead of silently collapsed;
* a label patch's derived ``add`` events get the new actor and the result is
  written to a **new** tgz (never in place) and re-validated.

Everything DB-side runs in **one transaction** and ends with
``store.verify(...)`` before ``COMMIT``. The identity file and the new tgz are
staged next to their targets before the transaction and moved into place only
after it commits, so a rolled-back store leaves neither artifact ahead of it.

``--dry-run`` is the default and writes nothing at all. A second ``--apply``
finds no old names anywhere and reports ``unchanged`` without writing or backing
up. A cycle (``A`` -> ``B`` while ``B`` -> ``A``) is refused: a merge is
one-way, and a split needs its own plan.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import labelpatch
from .config import Config, load_config
from .store import (
    ACTOR_RE,
    connect,
    ensure_schema,
    imported_scope,
    summary,
    verify,
)
from .timeutil import iso, utcnow

#: ``--merge-suffixes``: four letters followed by an allocation suffix. The
#: store's pseudonym shape (``store.ACTOR_RE``) allows two initials per part, so
#: the base is always a valid pseudonym; the suffix is an artifact of two
#: humans colliding, never a distinct person.
SUFFIX_RE = re.compile(r"^([A-Za-z]{4})(\d+)$")
_ACTOR_RE = re.compile(ACTOR_RE)


class RekeyError(RuntimeError):
    """The requested mapping cannot be applied safely; nothing was written."""


# --------------------------------------------------------------------------- #
# mapping
# --------------------------------------------------------------------------- #
def _pseudonym(value: str, what: str) -> str:
    text = str(value or "").strip()
    if not _ACTOR_RE.fullmatch(text):
        raise RekeyError(f"{what} {value!r} is not a pseudonym shape ({ACTOR_RE})")
    return text


def validate_mapping(mapping: dict[str, str]) -> dict[str, str]:
    """Every source and target is a pseudonym; no self-map; no conflict."""
    out: dict[str, str] = {}
    for old, new in mapping.items():
        old = _pseudonym(old, "mapping source")
        new = _pseudonym(new, f"mapping target for {old}")
        if old == new:
            raise RekeyError(f"mapping {old}={new} maps a pseudonym to itself")
        if old in out and out[old] != new:
            raise RekeyError(
                f"{old} is mapped twice with different targets"
                f" ({out[old]!r} and {new!r})"
            )
        out[old] = new
    return out


def parse_mapping(items) -> dict[str, str]:
    """``--map OLD=NEW`` rows -> a validated dict, before anything is opened."""
    mapping: dict[str, str] = {}
    for item in items or ():
        old, separator, new = str(item).partition("=")
        if not separator:
            raise RekeyError(f"--map {item!r} is not OLD=NEW")
        old, new = old.strip(), new.strip()
        if old in mapping and mapping[old] != new:
            raise RekeyError(
                f"--map {old} is given twice with different targets"
                f" ({mapping[old]!r} and {new!r})"
            )
        mapping[old] = new
    return validate_mapping(mapping)


def resolve_mapping(mapping: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Follow chains to their end; refuse a cycle.

    ``A -> B`` plus ``B -> C`` is not ambiguous, it is one merge into ``C``: the
    repair is symmetric, so resolving transitively is both deterministic and
    idempotent. A cycle has no end, and applying it in any order would either
    half-merge or loop, so it is refused outright -- never "applied blindly".
    """
    resolved: dict[str, str] = {}
    notes: list[str] = []
    for old in mapping:
        seen = [old]
        current = old
        while current in mapping:
            following = mapping[current]
            if following in seen:
                cycle = seen[seen.index(following):] + [following]
                raise RekeyError(
                    "cycle in the mapping: " + " -> ".join(cycle)
                    + "; a rekey is a one-way merge/rename, so nothing was written"
                )
            seen.append(following)
            current = following
        resolved[old] = current
        if len(seen) > 2:
            notes.append(
                f"{old} -> {mapping[old]} -> ... -> {current} (resolved transitively)"
            )
    return resolved, notes


def _suffix_candidates(con, extra_pseudonyms: set[str]) -> dict[str, str]:
    """Every stored actor/person matching ``^([A-Za-z]{4})\\d+$`` -> its base.

    Candidates come from the store (event actors, object actors, PERSON ids) and
    from the raw-zone identity map, which is exactly where the 2026-11-23 note
    says the old laptop pseudonyms still live. A base that is not itself a valid
    pseudonym is skipped; the suffix rule never invents an identity.
    """
    names: set[str] = set(extra_pseudonyms)
    names.update(row[0] for row in con.execute("SELECT DISTINCT actor FROM events").fetchall())
    names.update(
        row[0]
        for row in con.execute(
            "SELECT DISTINCT actor FROM objects WHERE actor IS NOT NULL"
        ).fetchall()
    )
    names.update(
        str(row[0]).partition(":")[2]
        for row in con.execute(
            "SELECT object_id FROM objects WHERE object_type = 'PERSON'"
        ).fetchall()
    )
    mapping: dict[str, str] = {}
    for name in sorted(str(n) for n in names if n):
        match = SUFFIX_RE.fullmatch(name)
        if not match:
            continue
        base = match.group(1)
        if not _ACTOR_RE.fullmatch(base):
            continue
        mapping[name] = base
    return mapping


# --------------------------------------------------------------------------- #
# evidence (dry-run table + apply report)
# --------------------------------------------------------------------------- #
def _scalar(con, sql: str, params: list) -> int:
    return int(con.execute(sql, params).fetchone()[0] or 0)


def _ticket_keys(con, object_type: str, actor: str) -> set[str]:
    out: set[str] = set()
    for (keys,) in con.execute(
        "SELECT ticket_keys FROM objects"
        " WHERE object_type = ? AND actor = ? AND ticket_keys IS NOT NULL",
        [object_type, actor],
    ).fetchall():
        out.update(str(key) for key in (keys or ()))
    return out


def _kinds(con, actor: str) -> set[str]:
    return {
        str(row[0])
        for row in con.execute(
            "SELECT DISTINCT CAST(attributes->>'actor_kind' AS VARCHAR) FROM events"
            " WHERE actor = ? AND attributes->>'actor_kind' IS NOT NULL",
            [actor],
        ).fetchall()
    }


def _person_row(con, actor: str) -> dict | None:
    row = con.execute(
        "SELECT CAST(attributes AS VARCHAR) FROM objects"
        " WHERE object_type = 'PERSON' AND object_id = ?",
        [f"PERSON:{actor}"],
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0] or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}


def _evidence(con, old: str, new: str, source: str, requested: str) -> dict:
    """One mapping's evidence: the numbers the hand SQL was reasoned from."""
    bb_events = _scalar(
        con, "SELECT count(*) FROM events WHERE actor = ? AND repo IS NOT NULL", [old]
    )
    jira_events = _scalar(
        con, "SELECT count(*) FROM events WHERE actor = ? AND repo IS NULL", [old]
    )
    person_links = _scalar(
        con,
        "SELECT count(*) FROM event_objects"
        " WHERE object_type = 'PERSON' AND object_id = ?",
        [f"PERSON:{old}"],
    )
    objects_actor = _scalar(con, "SELECT count(*) FROM objects WHERE actor = ?", [old])
    prs_old = _scalar(
        con, "SELECT count(*) FROM objects WHERE object_type = 'PR' AND actor = ?", [old]
    )
    prs_new = _scalar(
        con, "SELECT count(*) FROM objects WHERE object_type = 'PR' AND actor = ?", [new]
    )
    stories_old = _scalar(
        con, "SELECT count(*) FROM objects WHERE object_type = 'STORY' AND actor = ?", [old]
    )
    stories_new = _scalar(
        con, "SELECT count(*) FROM objects WHERE object_type = 'STORY' AND actor = ?", [new]
    )
    shared = sorted(_ticket_keys(con, "PR", old) & _ticket_keys(con, "STORY", new))
    old_row, new_row = _person_row(con, old), _person_row(con, new)
    warnings: list[str] = []
    if requested != new:
        warnings.append(f"resolved through {requested}")
    if old_row is None and new_row is None and not (
        bb_events or jira_events or person_links or objects_actor
    ):
        warnings.append("not present in the store")
    old_kind = (old_row or {}).get("actor_kind")
    new_kind = (new_row or {}).get("actor_kind")
    if old_kind and new_kind and old_kind != new_kind:
        warnings.append(
            f"actor_kind conflict: {old} is {old_kind!r}, {new} is {new_kind!r}"
            " (verify will refuse the merge)"
        )
    kinds = _kinds(con, old) | _kinds(con, new)
    if len(kinds) > 1:
        warnings.append(
            f"events classify {old}/{new} as both {sorted(kinds)}"
            " (verify will refuse the merge)"
        )
    if SUFFIX_RE.fullmatch(new):
        warnings.append(f"target {new} still carries a numeric suffix")
    return {
        "old": old,
        "new": new,
        "source": source,
        "requested": requested,
        "bb_events": bb_events,
        "jira_events": jira_events,
        "events": bb_events + jira_events,
        "person_links": person_links,
        "objects_actor": objects_actor,
        "prs_old": prs_old,
        "prs_new": prs_new,
        "stories_old": stories_old,
        "stories_new": stories_new,
        "shared_ticket_keys": shared,
        "person_old": old_row is not None,
        "person_new": new_row is not None,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------- #
# stores: one mapping inside the transaction
# --------------------------------------------------------------------------- #
def _actor_kind(con, actor: str) -> str | None:
    row = _person_row(con, actor) or {}
    if row.get("actor_kind"):
        return str(row["actor_kind"])
    kinds = _kinds(con, actor)
    return next(iter(kinds)) if len(kinds) == 1 else None


def _apply_mapping(con, old: str, new: str) -> dict:
    """Move one pseudonym across links, actor columns and the PERSON row."""
    old_id, new_id = f"PERSON:{old}", f"PERSON:{new}"
    stats = {
        "links": 0,
        "links_deduped": 0,
        "events": 0,
        "objects": 0,
        "person": "none",  # merged | renamed | created | none
    }
    kind = _actor_kind(con, old) or _actor_kind(con, new)

    # 1. PERSON links. An event may already link the target (a merge of two
    #    review approvals), so drop the row that would collide first: the
    #    relation's primary key is the triple, and UPDATE into an existing row
    #    would violate it.
    stats["links_deduped"] = _scalar(
        con,
        "SELECT count(*) FROM event_objects WHERE object_type = 'PERSON'"
        " AND object_id = ? AND event_key IN ("
        "  SELECT event_key FROM event_objects"
        "  WHERE object_type = 'PERSON' AND object_id = ?)",
        [new_id, old_id],
    )
    if stats["links_deduped"]:
        con.execute(
            "DELETE FROM event_objects WHERE object_type = 'PERSON'"
            " AND object_id = ? AND event_key IN ("
            "  SELECT event_key FROM event_objects"
            "  WHERE object_type = 'PERSON' AND object_id = ?)",
            [new_id, old_id],
        )
    stats["links"] = _scalar(
        con,
        "SELECT count(*) FROM event_objects WHERE object_type = 'PERSON' AND object_id = ?",
        [old_id],
    )
    if stats["links"]:
        con.execute(
            "UPDATE event_objects SET object_id = ?"
            " WHERE object_type = 'PERSON' AND object_id = ?",
            [new_id, old_id],
        )

    # 2. Actor columns: the event log's actor and every object's actor role.
    stats["events"] = _scalar(con, "SELECT count(*) FROM events WHERE actor = ?", [old])
    if stats["events"]:
        con.execute("UPDATE events SET actor = ? WHERE actor = ?", [new, old])
    stats["objects"] = _scalar(con, "SELECT count(*) FROM objects WHERE actor = ?", [old])
    if stats["objects"]:
        con.execute("UPDATE objects SET actor = ? WHERE actor = ?", [new, old])

    # 3. The PERSON row: merge into the target, or rename it into the base
    #    (that is how a missing base is created), or create a row only when the
    #    target is already referenced.
    old_row = con.execute(
        "SELECT CAST(attributes AS VARCHAR) FROM objects"
        " WHERE object_type = 'PERSON' AND object_id = ?",
        [old_id],
    ).fetchone()
    new_row = con.execute(
        "SELECT 1 FROM objects WHERE object_type = 'PERSON' AND object_id = ?",
        [new_id],
    ).fetchone()
    if old_row is not None and new_row is not None:
        old_attrs = json.loads(old_row[0] or "{}")
        if old_attrs.get("actor_kind") and not (_person_row(con, new) or {}).get("actor_kind"):
            con.execute(
                "UPDATE objects SET attributes = CAST(? AS JSON)"
                " WHERE object_type = 'PERSON' AND object_id = ?",
                [
                    json.dumps(
                        {**(_person_row(con, new) or {}), "actor_kind": old_attrs["actor_kind"]},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    new_id,
                ],
            )
        con.execute(
            "DELETE FROM objects WHERE object_type = 'PERSON' AND object_id = ?", [old_id]
        )
        stats["person"] = "merged"
    elif old_row is not None:
        con.execute(
            "UPDATE objects SET object_id = ?, actor = ?"
            " WHERE object_type = 'PERSON' AND object_id = ?",
            [new_id, new, old_id],
        )
        stats["person"] = "renamed"
    else:
        referenced = _scalar(
            con,
            "SELECT (SELECT count(*) FROM event_objects WHERE object_id = ?)"
            " + (SELECT count(*) FROM events WHERE actor = ?)"
            " + (SELECT count(*) FROM objects WHERE actor = ?"
            "    AND NOT (object_type = 'PERSON' AND object_id = ?))",
            [new_id, new, new, new_id],
        )
        if referenced:
            attributes = {"actor_kind": kind} if kind else {}
            con.execute(
                "INSERT INTO objects (object_type, object_id, actor, attributes)"
                " VALUES ('PERSON', ?, ?, CAST(? AS JSON))",
                [
                    new_id,
                    new,
                    json.dumps(attributes, separators=(",", ":"), sort_keys=True),
                ],
            )
            stats["person"] = "created"
    return stats


def _sweep_orphan_persons(con) -> int:
    """Delete PERSON rows nothing refers to any more.

    "Reference" deliberately includes ``events.actor`` and ``objects.actor``,
    not only ``event_objects``: a PERSON row with no link is still a person (the
    fixture store has two, and every actor seen gets a row), so a link-only rule
    would wipe most of the roster. A row whose pseudonym appears nowhere *is*
    dead -- the pre-existing orphans the 2026-11-23 repair also removed.
    """
    where = (
        "object_type = 'PERSON'"
        " AND NOT EXISTS (SELECT 1 FROM event_objects eo"
        "  WHERE eo.object_type = 'PERSON' AND eo.object_id = objects.object_id)"
        " AND NOT EXISTS (SELECT 1 FROM events e WHERE e.actor = objects.actor)"
        " AND NOT EXISTS (SELECT 1 FROM objects x"
        "  WHERE x.actor = objects.actor AND x.object_id <> objects.object_id)"
    )
    dead = _scalar(con, f"SELECT count(*) FROM objects WHERE {where}", [])
    if dead:
        con.execute(f"DELETE FROM objects WHERE {where}")
    return dead


# --------------------------------------------------------------------------- #
# identity map (raw zone)
# --------------------------------------------------------------------------- #
def _read_identity(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise RekeyError(f"identity map {path} is not readable JSON ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("actors"), dict):
        raise RekeyError(
            f"identity map {path} has no `actors` object (expected the raw-zone"
            " identity table this scrape writes)"
        )
    return data


def identity_pseudonyms(data: dict | None) -> set[str]:
    if not data:
        return set()
    return {
        str(entry.get("pseudonym"))
        for entry in data["actors"].values()
        if isinstance(entry, dict) and entry.get("pseudonym")
    }


def _identity_plan(path: Path, data: dict, mapping: dict[str, str]) -> dict:
    """Rewrite each stored pseudonym that is a mapping key; report duplicates."""
    actors = copy.deepcopy(data["actors"])
    updated: list[str] = []
    missing = 0
    for key, entry in actors.items():
        if not isinstance(entry, dict):
            missing += 1
            continue
        pseudonym = entry.get("pseudonym")
        if pseudonym in mapping and mapping[pseudonym] != pseudonym:
            entry["pseudonym"] = mapping[pseudonym]
            updated.append(str(key))
    by_target: dict[str, list[str]] = {}
    for key, entry in actors.items():
        if isinstance(entry, dict) and entry.get("pseudonym"):
            by_target.setdefault(str(entry["pseudonym"]), []).append(str(key))
    duplicates = [
        {"pseudonym": pseudonym, "keys": sorted(keys)}
        for pseudonym, keys in sorted(by_target.items())
        if len(keys) > 1
    ]
    return {
        "path": str(path),
        "updated": len(updated),
        "updated_keys": sorted(updated),
        "missing_entries": missing,
        "duplicates": duplicates,
        "data": actors,
    }


def _write_identity(data: dict, actors: dict, target: Path) -> None:
    payload = {**data, "actors": actors}
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- #
# label patch
# --------------------------------------------------------------------------- #
def _patch_plan(patch, mapping: dict[str, str]) -> tuple[list[dict], int]:
    """Copy the patch rows, remap the derived ``add`` events' actors."""
    rows = copy.deepcopy(patch.rows)
    remapped = 0
    for row in rows:
        for event in row.get("events") or ():
            actor = event.get("actor")
            if actor in mapping and mapping[actor] != actor:
                event["actor"] = mapping[actor]
                remapped += 1
    return rows, remapped


def _already_rekeyed(out: Path, patch, mapping: dict[str, str]) -> bool:
    """Is ``--out`` already the rekeyed copy of this patch?

    The input tgz is immutable by design, so a second identical ``--apply``
    would otherwise find the same old actors in it forever and call itself a
    change. The output we wrote names its source digest in the manifest; if that
    matches and no mapped actor is left, the patch half is already done.
    """
    if not out.is_file():
        return False
    try:
        existing = labelpatch.read_patch(out)
    except labelpatch.PatchError:
        return False
    meta = existing.manifest.get("rekey") or {}
    if meta.get("from_sha256") != patch.sha256:
        return False
    return not any(
        event.get("actor") in mapping
        for row in existing.rows
        for event in row.get("events") or ()
    )


def _stage_patch(
    patch, rows: list[dict], mapping: dict[str, str], remapped: int, target: Path
) -> dict:
    """Write the rekeyed tgz to ``target`` (a temp path) and re-validate it."""
    manifest = copy.deepcopy(patch.manifest)
    manifest["rekey"] = {
        "at": iso(utcnow()),
        "from_sha256": patch.sha256,
        "mappings": len(mapping),
        "events_remapped": int(remapped),
    }
    written = labelpatch.write_patch(target, manifest=manifest, rows=rows)
    validated = labelpatch.read_patch(target)
    leftover = [
        event["actor"]
        for row in validated.rows
        for event in row.get("events") or ()
        if event.get("actor") in mapping
    ]
    if leftover:
        raise RekeyError(
            f"rekeyed patch {target} still carries mapped actor(s) {sorted(set(leftover))}"
        )
    if len(validated.rows) != len(patch.rows):
        raise RekeyError(
            f"rekeyed patch {target} holds {len(validated.rows)} row(s),"
            f" the source holds {len(patch.rows)}"
        )
    return {
        "path": str(patch.path),
        "rows": len(validated.rows),
        "events_remapped": int(remapped),
        "sha256": written["sha256"],
        "validated": True,
    }


# --------------------------------------------------------------------------- #
# backup
# --------------------------------------------------------------------------- #
def _backup_path(db: Path) -> Path:
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    candidate = Path(f"{db}.pre-rekey-{stamp}.duckdb")
    n = 2
    while candidate.exists():
        candidate = Path(f"{db}.pre-rekey-{stamp}-{n}.duckdb")
        n += 1
    return candidate


def _backup(db: Path) -> Path:
    target = _backup_path(db)
    shutil.copy2(db, target)
    return target


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #
def rekey(
    cfg: Config,
    *,
    db_path: str | Path,
    identity_path: str | Path | None = None,
    explicit: dict[str, str] | None = None,
    merge_suffixes: bool = False,
    patch_path: str | Path | None = None,
    out_path: str | Path | None = None,
    apply: bool = False,
    backup: bool = True,
) -> dict:
    """Plan (default) or apply the identity repair; return a report."""
    db = Path(db_path).expanduser()
    if not db.is_file():
        raise RekeyError(f"no store at {db}")
    identity = Path(identity_path).expanduser() if identity_path else None
    if identity is not None and not identity.is_file():
        raise RekeyError(f"no identity map at {identity}")
    patch_file = Path(patch_path).expanduser() if patch_path else None
    out = Path(out_path).expanduser() if out_path else None
    if patch_file is not None and out is None:
        raise RekeyError("--patch needs --out (the input tgz is never rewritten in place)")
    if out is not None and patch_file is None:
        raise RekeyError("--out only makes sense with --patch")
    if patch_file is not None and out is not None and out.resolve() == patch_file.resolve():
        raise RekeyError(f"--out {out} is the input patch; rekey writes a new tgz")

    source = {old: "explicit" for old in (explicit or {})}
    mapping = validate_mapping(dict(explicit or {}))
    identity_data = _read_identity(identity) if identity is not None else None

    # Plan on a read-only connection: a dry run must not write a byte.
    con = connect(db, read_only=True)
    try:
        if merge_suffixes:
            for old, base in _suffix_candidates(con, identity_pseudonyms(identity_data)).items():
                if old not in mapping:
                    mapping[old] = base
                    source[old] = "suffix"
        mapping, notes = resolve_mapping(mapping)
        if not mapping:
            report = {
                "mode": "rekey",
                "dry_run": not apply,
                "db": str(db),
                "mappings": [],
                "identity": None,
                "patch": None,
                "backup": None,
                "changed": False,
                "unchanged": True,
                "notes": ["no mappings: nothing matches --merge-suffixes or --map"],
                "verified": None,
                "problems": [],
                "summary": summary(con),
            }
            return report

        plan = [
            _evidence(con, old, new, source[old], (explicit or {}).get(old, new))
            for old, new in sorted(mapping.items())
        ]
        patch = labelpatch.read_patch(patch_file) if patch_file is not None else None
        patch_rows, patch_remapped = _patch_plan(patch, mapping) if patch else ([], 0)
        patch_already = bool(
            patch is not None
            and patch_remapped
            and out is not None
            and _already_rekeyed(out, patch, mapping)
        )
        if patch_already:
            patch_remapped = 0
        identity_plan = (
            _identity_plan(identity, identity_data, mapping)
            if identity is not None and identity_data is not None
            else None
        )
        before = summary(con)
        would_change = bool(
            patch_remapped
            or (identity_plan and identity_plan["updated"])
            or any(
                row["events"]
                or row["objects_actor"]
                or row["person_links"]
                or row["person_old"]
                for row in plan
            )
        )
    finally:
        con.close()

    report = {
        "mode": "rekey",
        "dry_run": not apply,
        "db": str(db),
        "mappings": plan,
        "identity": (
            {key: value for key, value in identity_plan.items() if key != "data"}
            if identity_plan
            else None
        ),
        "patch": (
            {
                "path": str(patch.path),
                "out": str(out),
                "rows": len(patch.rows),
                "events_remapped": patch_remapped,
                "already_rekeyed": patch_already,
                "written": False,
            }
            if patch is not None
            else None
        ),
        "backup": None,
        "changed": would_change,
        "unchanged": not would_change,
        "notes": notes,
        "verified": None,
        "problems": [],
        "summary": before,
    }
    if not apply or not would_change:
        return report

    # ---- apply ---------------------------------------------------------
    if backup:
        report["backup"] = str(_backup(db))

    stamp = f".rekey-{os.getpid()}"
    staged: list[tuple[Path, Path]] = []
    try:
        if identity_plan and identity_plan["updated"]:
            identity_tmp = identity.with_name(identity.name + stamp)
            _write_identity(identity_data, identity_plan["data"], identity_tmp)
            staged.append((identity_tmp, identity))
            report["identity"]["written"] = True
        if patch is not None and patch_remapped:
            out.parent.mkdir(parents=True, exist_ok=True)
            patch_tmp = out.with_name(out.name + stamp)
            staged_patch = _stage_patch(
                patch, patch_rows, mapping, patch_remapped, patch_tmp
            )
            staged.append((patch_tmp, out))
            report["patch"].update(staged_patch)
            report["patch"]["out"] = str(out)
            report["patch"]["written"] = True

        con = connect(db)
        try:
            ensure_schema(con)
            keys = cfg.ticket_keys or imported_scope(con)
            con.execute("BEGIN")
            try:
                for old, new in sorted(mapping.items()):
                    result = _apply_mapping(con, old, new)
                    next(
                        row for row in report["mappings"] if row["old"] == old
                    )["result"] = result
                report["orphan_persons"] = _sweep_orphan_persons(con)
                check = verify(con, cfg.key_shape, cfg.ticket_projects, keys)
                report["verified"] = bool(check["ok"])
                report["problems"] = list(check["problems"])
                if not check["ok"]:
                    raise RekeyError(
                        "rekeyed store failed the pseudonymisation contract; rolled"
                        " back (no change kept):\n  - "
                        + "\n  - ".join(check["problems"])
                    )
                con.execute("COMMIT")
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass
                raise
            report["summary"] = summary(con)
        finally:
            con.close()

        for source_path, target_path in staged:
            os.replace(source_path, target_path)
    except Exception:
        for source_path, _target in staged:
            try:
                source_path.unlink()
            except OSError:
                pass
        raise
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser(prog: str = "python3 -m ingest rekey") -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Merge/rekey pseudonyms across the store, the identity map and a"
                    " label patch, in one transaction (dry run by default).",
    )
    ap.add_argument("--db", default=None, metavar="DB", help="store path (or MEGASPEED_DB)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--identity", default=None, metavar="PATH",
                    help="raw-zone identity.json whose stored pseudonyms are updated")
    ap.add_argument("--map", action="append", default=[], metavar="OLD=NEW",
                    help="explicit alias/merge (repeatable)")
    ap.add_argument("--merge-suffixes", action="store_true",
                    help="map every ^([A-Za-z]{4})\\d+$ actor/person to its base")
    ap.add_argument("--patch", default=None, metavar="TAGS.tgz",
                    help="label patch whose derived add events get the new actor")
    ap.add_argument("--out", default=None, metavar="NEW.tgz",
                    help="new tgz to write (required with --patch; never in place)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="print the evidence table and write nothing (default)")
    mode.add_argument("--apply", action="store_true",
                      help="back up the store, run one transaction, verify, commit")
    ap.add_argument("--no-backup", action="store_true",
                    help="with --apply: do not copy the store first")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    return ap


def _print_report(report: dict) -> None:
    head = "dry run" if report["dry_run"] else "apply"
    print(f"store          {report['db']}   ({head})")
    if report.get("backup"):
        print(f"backup         {report['backup']}")
    if not report["mappings"]:
        print("mappings       none")
    for row in report["mappings"]:
        print(f"mapping        {row['old']} → {row['new']}   ({row['source']})")
        print(
            f"               events BB {row['bb_events']} · Jira {row['jira_events']}"
            f" · PERSON links {row['person_links']}"
        )
        print(
            f"               PRs {row['old']}={row['prs_old']} → {row['new']}={row['prs_new']}"
            f" · stories {row['old']}={row['stories_old']} → {row['new']}={row['stories_new']}"
            f" · PERSON {row['person_old']}→{row['person_new']}"
        )
        if row["shared_ticket_keys"]:
            shown = ", ".join(row["shared_ticket_keys"][:6])
            more = " …" if len(row["shared_ticket_keys"]) > 6 else ""
            print(f"               shared ticket keys {shown}{more}")
        for warning in row.get("warnings") or ():
            print(f"warning        {row['old']} → {row['new']}: {warning}")
        if row.get("result"):
            result = row["result"]
            print(
                f"applied        {result['events']} event(s), {result['objects']} object"
                f" actor(s), {result['links']} link(s)"
                + (f", {result['links_deduped']} duplicate link(s) dropped"
                   if result["links_deduped"] else "")
                + f", PERSON {result['person']}"
            )
    identity = report.get("identity")
    if identity:
        print(
            f"identity       {identity['path']} · {identity['updated']} pseudonym(s)"
            f" {'updated' if identity.get('written') else 'to update'}"
        )
        for duplicate in identity["duplicates"][:4]:
            print(
                f"duplicate      {duplicate['pseudonym']} is shared by"
                f" {', '.join(duplicate['keys'][:4])}"
                + (" …" if len(duplicate["keys"]) > 4 else "")
            )
    patch = report.get("patch")
    if patch:
        if patch.get("already_rekeyed") and not patch.get("written"):
            print(
                f"patch          {patch['path']} → {patch['out']}"
                " · already rekeyed"
            )
        else:
            print(
                f"patch          {patch['path']} → {patch['out']}"
                f" · {patch['events_remapped']} derived event actor(s)"
                f" {'remapped' if patch.get('written') else 'to remap'}"
                + (f" · sha256 {patch['sha256'][:16]}…" if patch.get("sha256") else "")
            )
    for note in report.get("notes") or ():
        print(f"note           {note}")
    if report.get("orphan_persons"):
        print(f"orphans        {report['orphan_persons']} dead PERSON row(s) removed")
    if report["unchanged"]:
        print("plan           unchanged — nothing written")
    elif report["dry_run"]:
        print("plan           changes would be applied — dry run, nothing written")
    else:
        s = report["summary"]
        print(
            f"store now      {s['events']} events, {s['prs']} PRs,"
            f" as_of {s['as_of']}"
        )
    if report["verified"] is None:
        print(
            "contract       not checked"
            + (" (dry run)" if report["dry_run"] else " (nothing written)")
        )
    else:
        print(
            "contract       "
            + ("OK — no free text, no canaries" if report["verified"] else "VIOLATED")
        )


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    if not args.map and not args.merge_suffixes:
        print("nothing to do: give --map OLD=NEW and/or --merge-suffixes", file=sys.stderr)
        return 2
    if args.patch and not args.out:
        print("--patch needs --out NEW.tgz (the input patch is never rewritten)", file=sys.stderr)
        return 2
    if args.out and not args.patch:
        print("--out only makes sense with --patch", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    db = Path(args.db).expanduser() if args.db else cfg.db_path
    try:
        report = rekey(
            cfg,
            db_path=db,
            identity_path=args.identity,
            explicit=parse_mapping(args.map),
            merge_suffixes=args.merge_suffixes,
            patch_path=args.patch,
            out_path=args.out,
            apply=bool(args.apply),
            backup=not args.no_backup,
        )
    except (RekeyError, labelpatch.PatchError, OSError) as exc:
        print(f"rekey refused: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report)
    if report["verified"] is False:
        print("\nPSEUDONYMISATION CONTRACT VIOLATED:", file=sys.stderr)
        for problem in report["problems"]:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


__all__ = [
    "RekeyError",
    "build_parser",
    "identity_pseudonyms",
    "main",
    "parse_mapping",
    "rekey",
    "resolve_mapping",
]
