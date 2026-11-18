"""Measure the Jira scrape: checkpoint cost (full-bundle rewrite) vs N stories.

Clones the synthetic Jira fixture to N stories, then runs scrape_jira with
checkpoint_every=50 (default) and 0, timing both.
"""
import copy
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed")

from ingest import jira_fixtures as JF
from ingest.config import load_config
from ingest.scrape_jira import scrape as scrape_jira
from ingest.transport import FixtureJiraTransport


def fixture(n: int) -> dict:
    base = JF.generate(n=3)
    template = base["issues"][1]
    old_key = template["key"]
    issues = []
    for i in range(n):
        issue = copy.deepcopy(template)
        key = f"PROJ-{1000 + i}"
        text = json.dumps(issue).replace(old_key, key)
        issue = json.loads(text)
        issues.append(issue)
    payload = copy.deepcopy(base)
    payload["issues"] = issues
    payload["sprint_issues"] = {"1": [i["key"] for i in issues], "2": [], "3": []}
    return payload


def run(n: int, checkpoint_every: int):
    cfg = load_config(None)
    transport = FixtureJiraTransport(fixture(n))
    out = Path(f"/tmp/opencode/perf/bundles/jira{n}-cp{checkpoint_every}.tgz")
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    report = scrape_jira(
        cfg, transport, "jira-fixture", out=out,
        state_dir=out.parent / f"jstate-{n}-{checkpoint_every}",
        full=True, checkpoint_every=checkpoint_every, progress=None,
    )
    return time.perf_counter() - t0, report


print(f"{'n':>5} {'cp=50 s':>9} {'cp=0 s':>9} {'overhead':>9} {'bundle KB':>10} {'events':>8} {'reqs':>6}")
for n in (250, 500, 1000, 2000):
    t50, r50 = run(n, 50)
    t0, r0 = run(n, 0)
    print(f"{n:>5} {t50:>9.2f} {t0:>9.2f} {t50/max(t0,1e-9):>8.1f}x "
          f"{r50['bundle_bytes']/1024:>10.0f} {r50['events']:>8} {r50['requests']:>6}")
