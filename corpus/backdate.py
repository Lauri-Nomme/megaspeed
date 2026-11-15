#!/usr/bin/env python3
"""Rewrite Bitbucket's PR/comment/approval timestamps to the planned timeline.

Bitbucket DC's REST API has no way to backdate PR events: ``createdDate`` is
always "now".  The lab instance is ours, so this script edits the embedded H2
database directly:

1. stop ``bb-lab-b``
2. copy ``shared/data/db.mv.db`` to ``corpus/build/backdate/`` (original kept
   as a backup next to the copy)
3. dump pull requests / activities / comments to CSV with H2's CSVWRITE
4. map every PR activity row to the planned event of the same kind (opened,
   comment, rescope, approve, merged/declined) and rewrite:
     * STA_ACTIVITY.CREATED_TIMESTAMP
     * BB_COMMENT.CREATED_TIMESTAMP / UPDATED_TIMESTAMP (+ thread timestamps)
     * STA_PULL_REQUEST.CREATED/UPDATED/CLOSED/RESCOPED_TIMESTAMP
5. copy the database back, restart the container and verify through REST

Run it once after ``generate.py``.  It is idempotent: mapping is derived from
the plan, not from the current timestamps, so a rerun produces the same data.

    python3 corpus/backdate.py            # apply
    python3 corpus/backdate.py --verify   # only verify (container must be up)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import base64
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
PLAN_FILE = BUILD / "plan.json"
WORK = BUILD / "backdate"
LAB_HOME = Path(os.environ.get(
    "BB_LAB_HOME", "/vokk/home/lauri/dev/hans/lab/home/bb-lab-b"))
DB_REL = Path("shared/data/db.mv.db")
CONTAINER = os.environ.get("BB_CONTAINER", "bb-lab-b")
NERDCTL = os.environ.get("NERDCTL_CMD", "sudo nerdctl")
H2_JAR = Path(os.environ.get("H2_JAR", "/tmp/opencode/h2-shell.jar"))
BASE = os.environ.get("BB_BASE", "http://127.0.0.1:7991").rstrip("/")
ADMIN = ("admin", "admin-lab-pw")
DB_OWNER = os.environ.get("BB_DB_OWNER", "2003:2003")
UTC = timezone.utc

# STA_PR_ACTIVITY.PR_ACTION values (decoded from the lab database)
A_DECLINED, A_COMMENTED, A_MERGED, A_OPENED, A_RESCOPED, A_UPDATED, A_APPROVED, A_REVIEWED = (
    0, 1, 2, 3, 5, 6, 7, 9)
KIND_BY_ACTION = {
    A_OPENED: "opened", A_COMMENTED: "commented", A_MERGED: "merged",
    A_DECLINED: "declined", A_RESCOPED: "rescoped", A_APPROVED: "approved",
    A_UPDATED: "updated", A_REVIEWED: "reviewed",
}


def log(msg: str) -> None:
    print(f"[backdate] {msg}", flush=True)


def sh(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({p.returncode}):\n{p.stderr or p.stdout}")
    return p


def nerdctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return sh(NERDCTL.split() + list(args), check=check)


def parse_ts(s: str) -> datetime:
    s = s.strip().strip('"')
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    raise ValueError(f"bad timestamp {s!r}")


def sql_ts(dt: datetime | None) -> str:
    if dt is None:
        return "NULL"
    return "TIMESTAMP '{}'".format(dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])


def run_sql(db: Path, script: str) -> None:
    """Run a script (string) against the H2 file database."""
    path = WORK / "_script.sql"
    path.write_text(script)
    base = str(db)[:-6] if str(db).endswith(".mv.db") else str(db)
    sh(["java", "-cp", str(H2_JAR), "org.h2.tools.RunScript",
        "-url", f"jdbc:h2:file:{base}", "-user", "sa", "-password", "",
        "-script", str(path)])


def dump(db: Path, name: str, select: str) -> Path:
    out = WORK / f"{name}.csv"
    out.unlink(missing_ok=True)
    run_sql(db, f"CALL CSVWRITE('{out}', '{select}');")
    return out


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
def plan_events(pr: dict) -> dict:
    """Planned timestamps per semantic event kind, in order.

    Events planned after the corpus window are clamped to the window end so a
    PR can never look like it was reviewed in the future.
    """
    actions = pr.get("actions") or []
    at_of = lambda a: min(parse_ts_iso(a["at"]), CUTOFF)
    comments = []
    for a in actions:  # a decline posts a comment too, before the decline
        if a["kind"] in ("comment", "needs_work", "decline"):
            comments.append(at_of(a))
    pushes = [at_of(a) for a in actions if a["kind"] == "push"]
    # One APPROVED row per approve transition; re-approving an already approved
    # reviewer is a no-op, but unapprove -> approve is two rows.
    approvals, approved = [], set()
    for a in actions:
        if a["kind"] == "approve" and a["actor"] not in approved:
            approved.add(a["actor"])
            approvals.append(at_of(a))
        elif a["kind"] == "unapprove":
            approved.discard(a["actor"])
    # DC writes one UPDATED activity per reviewer added/removed.  The two
    # reviewers added when the PR opens come first; a later reviewer swap adds
    # one removal + one addition at their planned times.
    reviewer_changes = [parse_ts_iso(pr["open_at"])] * len(pr.get("reviewers") or [])
    for a in actions:
        if a["kind"] in ("add_reviewer", "remove_reviewer"):
            reviewer_changes.append(at_of(a))
    resolves = [(int(a.get("thread", 0)), at_of(a)) for a in actions
                if a["kind"] == "resolve_thread"]
    closes = [at_of(a) for a in actions if a["kind"] in ("merge", "decline")]
    return {
        "opened": parse_ts_iso(pr["open_at"]),
        "comments": comments,
        "pushes": pushes,
        "approvals": approvals,
        "reviewer_changes": reviewer_changes,
        "resolves": resolves,
        "close": closes[-1] if closes else None,
    }


def parse_ts_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def main() -> int:
    ap = argparse.ArgumentParser(description="backdate Bitbucket PR events")
    ap.add_argument("--verify", action="store_true", help="only verify via REST")
    ap.add_argument("--db", default=str(LAB_HOME / DB_REL))
    args = ap.parse_args()

    if args.verify:
        return verify_only()

    if not H2_JAR.exists():
        log(f"ERROR: H2 jar not found at {H2_JAR}")
        return 2
    plan = json.loads(PLAN_FILE.read_text())["prs"]
    plan = [p for p in plan if p.get("pr_id") and p.get("timeline")]
    log(f"plan: {len(plan)} PRs with timelines")
    WORK.mkdir(parents=True, exist_ok=True)

    db_src = Path(args.db)
    if sh(["sudo", "test", "-f", str(db_src)], check=False).returncode != 0:
        log(f"ERROR: database not found (or not readable with sudo): {db_src}")
        return 2
    log(f"stopping {CONTAINER}")
    nerdctl("stop", CONTAINER)
    backup = WORK / "db.backup.mv.db"
    work_db = WORK / "db.mv.db"
    for f in (backup, work_db):
        f.unlink(missing_ok=True)
    sh(["sudo", "cp", str(db_src), str(backup)])
    sh(["sudo", "cp", str(db_src), str(work_db)])
    sh(["sudo", "chown", os.environ.get("USER", "lauri"), str(work_db), str(backup)])
    log(f"database copied to {work_db} (backup: {backup})")

    try:
        do_backdate(work_db, plan)
    except Exception:
        log("backdate failed - restoring database and restarting")
        sh(["sudo", "cp", str(backup), str(db_src)])
        sh(["sudo", "chown", DB_OWNER, str(db_src)])
        nerdctl("start", CONTAINER, check=False)
        raise

    sh(["sudo", "cp", str(work_db), str(db_src)])
    sh(["sudo", "chown", DB_OWNER, str(db_src)])
    log("updated database copied back")
    nerdctl("start", CONTAINER)
    log("container started, waiting for readiness")
    wait_ready()
    verify_rest(plan)
    log("done")
    return 0


def do_backdate(db: Path, plan: list[dict]) -> None:
    repos = {r["SLUG"]: int(r["ID"]) for r in read_csv(dump(
        db, "repos", "SELECT ID, SLUG FROM REPOSITORY"))}
    log(f"repositories: {repos}")
    prs = read_csv(dump(
        db, "prs",
        "SELECT ID, FROM_REPOSITORY_ID, FROM_BRANCH_NAME, PR_STATE, "
        "CREATED_TIMESTAMP, UPDATED_TIMESTAMP, CLOSED_TIMESTAMP, RESCOPED_TIMESTAMP "
        "FROM STA_PULL_REQUEST"))
    by_branch = {(int(p["FROM_REPOSITORY_ID"]), p["FROM_BRANCH_NAME"]): p for p in prs}
    acts = read_csv(dump(
        db, "acts",
        "SELECT pa.PR_ID, a.ID AS ACTIVITY_ID, pa.PR_ACTION, a.CREATED_TIMESTAMP "
        "FROM STA_PR_ACTIVITY pa JOIN STA_ACTIVITY a ON a.ID = pa.ACTIVITY_ID "
        "ORDER BY pa.PR_ID, a.CREATED_TIMESTAMP, a.ID"))
    acts_by_pr: dict[int, list[dict]] = {}
    for a in acts:
        acts_by_pr.setdefault(int(a["PR_ID"]), []).append(a)
    comments = read_csv(dump(
        db, "comments",
        "SELECT ba.ACTIVITY_ID, ba.COMMENT_ID, c.THREAD_ID, c.CREATED_TIMESTAMP "
        "FROM BB_PR_COMMENT_ACTIVITY ba JOIN BB_COMMENT c ON c.ID = ba.COMMENT_ID"))
    comment_by_activity = {int(c["ACTIVITY_ID"]): c for c in comments}
    thread_by_comment = {int(c["COMMENT_ID"]): int(c["THREAD_ID"]) for c in comments}

    updates: list[str] = []
    timeline_rows: list[str] = []
    resolved_threads: dict[int, datetime] = {}
    mapped = skipped = 0
    for pr in plan:
        slug = pr["repo"]
        repo_id = repos.get(slug)
        key = (repo_id, pr["branch"])
        row = by_branch.get(key)
        if not row:
            log(f"  WARN no DB row for {slug} {pr['branch']}")
            skipped += 1
            continue
        pr_id = int(row["ID"])
        rows = acts_by_pr.get(pr_id, [])
        ev = plan_events(pr)

        q_comments = deque(ev["comments"])
        q_pushes = deque(ev["pushes"])
        q_approvals = deque(ev["approvals"])
        q_reviewers = deque(ev["reviewer_changes"])
        times: list[tuple[int, datetime]] = []
        prev = ev["opened"]
        for a in rows:
            action = int(a["PR_ACTION"])
            t = None
            if action == A_OPENED:
                t = ev["opened"]
            elif action == A_COMMENTED:
                t = q_comments.popleft() if q_comments else prev
            elif action == A_RESCOPED:
                t = q_pushes.popleft() if q_pushes else prev
            elif action == A_APPROVED:
                t = q_approvals.popleft() if q_approvals else prev
            elif action == A_UPDATED:
                # reviewer roster change: one row per added/removed reviewer
                t = q_reviewers.popleft() if q_reviewers else prev
            elif action in (A_MERGED, A_DECLINED):
                t = ev["close"] or prev
            else:  # REVIEWED / anything else: sit with its neighbour
                t = prev
            if t < prev:
                t = prev
            prev = t
            times.append((int(a["ACTIVITY_ID"]), t))

        if q_comments or q_pushes or q_approvals or q_reviewers:
            log(f"  WARN {pr['id']}: unmatched planned events "
                f"(comments={len(q_comments)} pushes={len(q_pushes)} "
                f"approvals={len(q_approvals)} reviewers={len(q_reviewers)})")

        last = prev
        rescoped = None
        for (aid, t), a in zip(times, rows):
            if int(a["PR_ACTION"]) == A_RESCOPED:
                rescoped = t
        close = ev["close"] if int(row["PR_STATE"]) in (1, 2) else None

        for aid, t in times:
            updates.append(f"UPDATE STA_ACTIVITY SET CREATED_TIMESTAMP = {sql_ts(t)} "
                           f"WHERE ID = {aid};")
            c = comment_by_activity.get(aid)
            if c:
                updates.append(f"UPDATE BB_COMMENT SET CREATED_TIMESTAMP = {sql_ts(t)}, "
                               f"UPDATED_TIMESTAMP = {sql_ts(t)} "
                               f"WHERE ID = {int(c['COMMENT_ID'])};")

        # Thread resolution happens through PUT .../comments/{id}; DC records no
        # extra activity, only the comment/thread resolved timestamp.
        thread_ids = pr.get("thread_ids") or []
        for thread_index, when in ev["resolves"]:
            if thread_index >= len(thread_ids):
                continue
            cid = int(thread_ids[thread_index])
            tid = thread_by_comment.get(cid)
            updates.append(f"UPDATE BB_COMMENT SET RESOLVED_TIMESTAMP = {sql_ts(when)} "
                           f"WHERE ID = {cid};")
            if tid:
                resolved_threads[tid] = when

        # The MERGED activity row exists with a NULL hash for a detected merge;
        # point it at the backdated local merge commit so the REST activity (and
        # the ingest's merge_commit extraction) carries a real commit block.
        merge_commit = pr.get("merge_commit")
        if merge_commit and re.fullmatch(r"[0-9a-f]{40}", str(merge_commit)):
            for (aid, _t), a in zip(times, rows):
                if int(a["PR_ACTION"]) == A_MERGED:
                    updates.append(
                        "UPDATE STA_PR_MERGE_ACTIVITY SET HASH = "
                        f"'{merge_commit}' WHERE ACTIVITY_ID = {aid};")

        updates.append(
            "UPDATE STA_PULL_REQUEST SET "
            f"CREATED_TIMESTAMP = {sql_ts(ev['opened'])}, "
            f"UPDATED_TIMESTAMP = {sql_ts(last)}, "
            f"CLOSED_TIMESTAMP = {sql_ts(close)}, "
            f"RESCOPED_TIMESTAMP = {sql_ts(rescoped or ev['opened'])} "
            f"WHERE ID = {pr_id};")
        timeline_rows.append(
            f"{pr['repo']}\t{pr['branch']}\t{pr_id}\t{len(rows)}\t"
            f"{ev['opened'].isoformat()}\t{last.isoformat()}")
        mapped += 1

    # thread timestamps follow their comments (min/max of the assigned times,
    # including the resolution timestamp)
    threads: dict[int, list[datetime]] = {}
    assigned = {}
    for line in updates:
        if line.startswith("UPDATE BB_COMMENT SET CREATED_TIMESTAMP = TIMESTAMP '"):
            value = line.split("TIMESTAMP '", 1)[1].split("'", 1)[0]
            cid = int(line.rsplit("ID = ", 1)[1].rstrip(";"))
            assigned[cid] = parse_ts(value)
    for c in comments:
        cid = int(c["COMMENT_ID"])
        if cid in assigned:
            threads.setdefault(int(c["THREAD_ID"]), []).append(assigned[cid])
    for tid, when in resolved_threads.items():
        threads.setdefault(tid, []).append(when)
    for tid, ts in threads.items():
        updates.append(f"UPDATE BB_COMMENT_THREAD SET CREATED_TIMESTAMP = {sql_ts(min(ts))}, "
                       f"UPDATED_TIMESTAMP = {sql_ts(max(ts))} WHERE ID = {tid};")
    for tid, when in resolved_threads.items():
        updates.append(f"UPDATE BB_COMMENT_THREAD SET RESOLVED_TIMESTAMP = {sql_ts(when)} "
                       f"WHERE ID = {tid};")

    log(f"mapped {mapped} PRs, skipped {skipped}, {len(updates)} SQL statements")
    (WORK / "timeline.tsv").write_text(
        "repo\tbranch\tdb_pr_id\tactivities\topened\tlast\n" + "\n".join(timeline_rows) + "\n")
    run_sql(db, "\n".join(updates))
    log("SQL applied")


def wait_ready(timeout: int = 600) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(BASE + "/status", timeout=5) as r:
                if "RUNNING" in r.read().decode():
                    break
        except Exception:
            pass
        time.sleep(5)
    else:
        raise RuntimeError("container did not report RUNNING")
    while time.time() < deadline:
        try:
            req = urllib.request.Request(BASE + "/rest/api/1.0/users/admin")
            req.add_header("Authorization", "Basic " +
                           base64.b64encode(b"admin:admin-lab-pw").decode())
            with urllib.request.urlopen(req, timeout=10) as r:
                if json.load(r).get("name") == "admin":
                    return
        except Exception:
            pass
        time.sleep(5)
    raise RuntimeError("sysadmin did not resolve after restart")


def api_get(path: str):
    req = urllib.request.Request(BASE + path)
    req.add_header("Authorization", "Basic " +
                   base64.b64encode(f"{ADMIN[0]}:{ADMIN[1]}".encode()).decode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def verify_rest(plan: list[dict]) -> None:
    sample = [p for p in plan if p["outcome"] == "merged"][:3] + \
             [p for p in plan if p["outcome"] == "open"][:1] + \
             [p for p in plan if p["outcome"] == "declined"][:1]
    ok = True
    for pr in sample:
        path = (f"/rest/api/1.0/projects/DEL/repos/{pr['repo']}/pull-requests/"
                f"{pr['pr_id']}")
        data = api_get(path)
        created = datetime.fromtimestamp(data["createdDate"] / 1000, UTC)
        expected = parse_ts_iso(pr["open_at"])
        delta = abs((created - expected).total_seconds())
        acts = api_get(path + "/activities?limit=100")["values"]
        first = datetime.fromtimestamp(acts[-1]["createdDate"] / 1000, UTC)
        last = datetime.fromtimestamp(acts[0]["createdDate"] / 1000, UTC)
        good = delta < 2 and first >= expected - timedelta(seconds=1) and last <= CUTOFF
        ok &= good
        log(f"  {pr['id']:12s} state={data['state']:8s} opened={created:%Y-%m-%d %H:%M} "
            f"(planned {expected:%Y-%m-%d %H:%M}) last={last:%Y-%m-%d %H:%M} "
            f"{'OK' if good else 'MISMATCH'}")
    if not ok:
        raise RuntimeError("REST verification failed")


CUTOFF = datetime(2026, 11, 13, 17, 0, tzinfo=UTC)


def verify_only() -> int:
    plan = [p for p in json.loads(PLAN_FILE.read_text())["prs"] if p.get("pr_id")]
    try:
        verify_rest(plan)
        return 0
    except Exception as e:
        log(f"verification failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
