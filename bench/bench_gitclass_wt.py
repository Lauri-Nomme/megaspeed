"""Per-PR git subprocess cost of the push_class / diff-size cache (worktree).

Builds a local bare "remote" with N feature branches, then runs the real
PushClassifier over N PR records, counting git subprocess invocations, fetches
and wall time. Cold cache (first scrape) and warm cache (second run) measured.
"""
import copy
import os
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

WT = sys.argv[2] if len(sys.argv) > 2 else "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape"
sys.path.insert(0, WT)
from ingest import gitclass

ROOT = Path("/tmp/opencode/perf/git")
REMOTE = ROOT / "remote/ENG/platform.git"
WORK = ROOT / "work"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
COMMITS_PER_BRANCH = 3
FILES = 40


def sh(cwd, *args, **kw):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True, **kw).stdout.strip()


def build_remote():
    if (REMOTE / "HEAD").exists():
        return
    if WORK.exists():
        sh(ROOT, "rm", "-rf", str(WORK))
    WORK.mkdir(parents=True)
    sh(WORK, "git", "init", "-q", "-b", "main", ".")
    sh(WORK, "git", "config", "user.email", "a@b.c")
    sh(WORK, "git", "config", "user.name", "a")
    rng = random.Random(7)
    for f in range(FILES):
        (WORK / f"f{f}.txt").write_text("\n".join(f"line {i}" for i in range(200)))
    sh(WORK, "git", "add", "-A")
    sh(WORK, "git", "commit", "-qm", "base")
    sh(WORK, "git", "tag", "base")
    for b in range(N):
        sh(WORK, "git", "checkout", "-q", "-b", f"feature/b{b}", "main")
        for c in range(COMMITS_PER_BRANCH):
            f = WORK / f"f{rng.randrange(FILES)}.txt"
            lines = f.read_text().splitlines()
            lines[rng.randrange(len(lines))] = f"branch {b} commit {c}"
            f.write_text("\n".join(lines))
            sh(WORK, "git", "commit", "-qam", f"b{b}c{c}")
        sh(WORK, "git", "checkout", "-q", "main")
    REMOTE.parent.mkdir(parents=True, exist_ok=True)
    sh(ROOT, "git", "clone", "-q", "--bare", str(WORK), str(REMOTE))


def pr_records(n):
    """(pr_record, activities, commits) per PR: two pushes each."""
    out = []
    base = sh(WORK, "git", "rev-parse", "base")
    for b in range(n):
        branch = f"feature/b{b}"
        head = sh(WORK, "git", "rev-parse", branch)
        old = sh(WORK, "git", "rev-parse", f"{branch}~1")
        pr_record = {
            "id": b + 1,
            "toRef": {"displayId": "main", "latestCommit": base},
            "fromRef": {"displayId": branch, "latestCommit": head},
        }
        activities = [{
            "action": "RESCOPED",
            "previousFromHash": old, "fromHash": head,
            "previousToHash": base, "toHash": base,
        }]
        commits = [{"id": head}]
        out.append((pr_record, activities, commits))
    return out


def bench(label, records, state, reset=False, classifier=None):
    if reset and state.exists():
        shutil.rmtree(state)
    records = copy.deepcopy(records)  # un-stamped: a fresh scrape's payload
    clf = classifier or gitclass.PushClassifier(
        state, f"file://{ROOT}/remote", url_template="{base}/{project}/{repo}.git"
    )
    before = clf.runs
    t0 = time.perf_counter()
    for pr, acts, commits in records:
        clf.annotate("ENG", "platform", pr, acts)
        clf.annotate_diff_size("ENG", "platform", pr, commits)
    elapsed = time.perf_counter() - t0
    n = len(records)
    runs = clf.runs - before
    print(f"{label:<30} {elapsed:7.2f} s {elapsed/n*1000:7.1f} ms/PR "
          f"git_runs={runs:6d} ({runs/n:5.1f}/PR) "
          f"fetches={clf.fetches:5d} classes={dict(clf.counts)}")
    return clf


build_remote()
print(f"remote: {N} branches x {COMMITS_PER_BRANCH} commits")
records = pr_records(N)
state = ROOT / "cache-wt"
clf = bench(f"cold cache, n={N}", records, state, reset=True)
bench(f"warm cache, n={N}", records, state)
bench(f"warm again, n={N}", records, state)
bench(f"same process, second pass", records, state, classifier=clf)
