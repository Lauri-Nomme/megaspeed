"""Dump insights + one ticket + the full event page so two checkouts can be diffed.

Usage: wt_dump_payloads.py <repo> <db> <out.json>
"""
import json
import sys
from pathlib import Path

REPO, DB, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, REPO)

from ingest import store as pstore
from analysis import ballcourt as analyse
from analysis import calendar as workcal
from analysis import jira as jira_analysis
from analysis import rules as insights_rules
from analysis import tickets as ticket_view
from server import query

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

out = {}
for level in ("team", "person"):
    out[f"insights:{level}"] = insights_rules.insights(
        court, threads, window, level=level, bots=bots, sizes=sizes, jira=jira
    )
out["decomposition"] = jira_analysis.waiting_time_decomposition(court, jira, window)
out["sprints"] = jira_analysis.sprints(court, jira, window)
out["carry_over"] = jira_analysis.carry_over(court, jira, window)
out["churn"] = jira_analysis.point_churn(court, jira, window)
out["worklog"] = jira_analysis.worklog_cross_check(court, jira, window)
out["conformance"] = jira_analysis.conformance(court, jira, window)

keys = [story.key for story in jira.stories][:3]
for key in keys:
    out[f"ticket:{key}"] = ticket_view.ticket(court, jira, key)

rows, total = query.list_events(con, None, None, None, 0, "asc")
out["events"] = rows
out["events_total"] = total

Path(OUT).write_text(json.dumps(out, sort_keys=True, default=str), encoding="utf-8")
print(f"{OUT}: {len(out)} payloads, {Path(OUT).stat().st_size} bytes")
