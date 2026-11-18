"""Count + time the redundant recomputations inside one insights payload.

Usage: wt_insights.py <repo> <db>
"""
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

cal = workcal.load(
    tenant="bench", tenants_dir=Path("/tmp/opencode/perf/tenants"),
    config_path=Path(REPO) / "config" / "calendar.default.yaml",
)
con = pstore.connect(DB, read_only=True)
court = analyse.load(con, calendar=cal)
jira = jira_analysis.load(con, court)
threads = insights_rules.load_threads(con)
bots = insights_rules.load_bots(con)
sizes = insights_rules.load_pr_sizes(con)
window = insights_rules.window_for(court, None, jira)

calls = {"business_hours": 0, "cycle": 0}
original_hours = workcal.Calendar.business_hours
original_cycle = jira_analysis._cycle


def counted_hours(self, *args, **kwargs):
    calls["business_hours"] += 1
    return original_hours(self, *args, **kwargs)


def counted_cycle(story):
    calls["cycle"] += 1
    return original_cycle(story)


workcal.Calendar.business_hours = counted_hours
jira_analysis._cycle = counted_cycle
# rules/jira call the module global; ballcourt calls the calendar method
times = []
for _ in range(3):
    calls["business_hours"] = calls["cycle"] = 0
    began = time.perf_counter()
    payload = insights_rules.insights(
        court, threads, window, level="team", bots=bots, sizes=sizes, jira=jira
    )
    times.append((time.perf_counter() - began) * 1000)
    print(f"  insights {times[-1]:8.1f} ms   business_hours={calls['business_hours']:7d}"
          f"   _cycle={calls['cycle']:6d}   stories={len(jira.stories)}")
workcal.Calendar.business_hours = original_hours
jira_analysis._cycle = original_cycle
print(f"median {statistics.median(times):.1f} ms")
