"""Bench the worktree's analysis/server hot paths. Usage: wt_bench.py <repo> <db>"""
import json
import statistics
import sys
import time
from pathlib import Path

REPO = sys.argv[1]
DB = sys.argv[2]
sys.path.insert(0, REPO)

from ingest import store as pstore
from analysis import ballcourt as analyse
from analysis import calendar as workcal
from analysis import jira as jira_analysis
from analysis import rules as insights_rules
from analysis import tickets as ticket_view
from server import query as query

cal = workcal.load(
    tenant="bench", tenants_dir=Path("/tmp/opencode/perf/tenants"),
    config_path=Path(REPO) / "config" / "calendar.default.yaml",
)


def timed(label, fn, repeat=3):
    times = []
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t0) * 1000)
    print(f"{label:<46} {statistics.median(times):9.1f} ms")
    return out


con = pstore.connect(DB, read_only=True)
summary = pstore.summary(con)
print(f"store {DB}: {summary['events']} events, {summary['prs']} PRs, {summary['stories']} stories\n")

court = timed("ballcourt.load", lambda: analyse.load(con, calendar=cal))
jira = timed("jira.load", lambda: jira_analysis.load(con, court))
threads = timed("rules.load_threads", lambda: insights_rules.load_threads(con))
bots = timed("rules.load_bots", lambda: insights_rules.load_bots(con))
sizes = timed("rules.load_pr_sizes", lambda: insights_rules.load_pr_sizes(con))
window = insights_rules.window_for(court, None, jira)

timed("queue_series person default", lambda: analyse.queue_series(court, "person", None, None), 2)
timed("queue_series person 1h", lambda: analyse.queue_series(court, "person", None, 1), 2)
timed("queue_series queue 1h", lambda: analyse.queue_series(court, "queue", None, 1), 2)

# fresh insights each time (the memo lives on the calendar/court, so rebuild)
def fresh_insights():
    c = analyse.load(con, calendar=cal)
    j = insights_rules.load_jira(con, c)
    w = insights_rules.window_for(c, None, j)
    return insights_rules.insights(c, threads, w, level="team", bots=bots, sizes=sizes, jira=j)


payload = timed("rules.insights (team, whole window)", fresh_insights, 2)
print(f"  -> payload {len(json.dumps(payload, default=str))} bytes")

# ticket endpoint path: load_jira + ticket()
def ticket_path():
    c = analyse.load(con, calendar=cal)
    d = insights_rules.load_jira(con, c)
    keys = [s.key for s in d.stories]
    return ticket_view.ticket(c, d, keys[0]) if keys else None


timed("/api/ticket path (load_jira + ticket)", ticket_path, 3)

# events page with all rows
timed("query.list_events(limit=200000)", lambda: query.list_events(con, None, None, 200_000, 0, "asc"), 2)
