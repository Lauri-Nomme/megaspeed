"""Time the analysis layer against a real store. Usage: bench_analysis.py <db> [--profile]"""
import json
import statistics
import sys
import time
import cProfile
import pstats
import io
from pathlib import Path

sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed")

from ingest import store as pstore
from analysis import ballcourt as analyse
from analysis import calendar as workcal
from analysis import jira as jira_analysis
from analysis import rules as insights_rules
from analysis import simulation as policy_lab
from analysis import tickets as ticket_view
from analysis import stack as stack_analysis

DB = sys.argv[1] if len(sys.argv) > 1 else "/vokk/home/lauri/dev/megaspeed/data/del-combined.duckdb"
PROFILE = "--profile" in sys.argv
REPEAT = 3

cal = workcal.load(
    tenant="bench", tenants_dir=Path("/tmp/opencode/perf/tenants"),
    config_path=Path("/vokk/home/lauri/dev/megaspeed/config/calendar.default.yaml"),
)


def timed(label, fn, repeat=REPEAT):
    times = []
    out = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t0) * 1000)
    print(f"{label:<46} {statistics.median(times):9.1f} ms   (runs: {', '.join(f'{t:.1f}' for t in times)})")
    return out


con = pstore.connect(DB, read_only=True)
summary = pstore.summary(con)
print(f"store {DB}: {summary['events']} events, {summary['prs']} PRs, {summary['stories']} stories\n")

court = timed("ballcourt.load", lambda: analyse.load(con, calendar=cal))
print(f"  -> {len(court.prs)} PR states, {len(court.intervals)} intervals")

jira = timed("jira.load", lambda: jira_analysis.load(con, court))
print(f"  -> {len(jira.stories)} stories, {jira.events} story events")

threads = timed("rules.load_threads", lambda: insights_rules.load_threads(con))
bots = timed("rules.load_bots", lambda: insights_rules.load_bots(con))
sizes = timed("rules.load_pr_sizes", lambda: insights_rules.load_pr_sizes(con))

window = insights_rules.window_for(court, None, jira)
print(f"window: {window[0]} .. {window[1]}")

payload = timed("rules.insights (team, whole window)",
                lambda: insights_rules.insights(court, threads, window, level="team", bots=bots, sizes=sizes, jira=jira))
print(f"  -> payload {len(json.dumps(payload, default=str))} bytes, {len(payload['suggestions'])} suggestions")

timed("rules.insights (person)", lambda: insights_rules.insights(
    court, threads, window, level="person", bots=bots, sizes=sizes, jira=jira))

for name, fn in [
    ("metrics() alone", lambda: insights_rules.metrics(court, threads, window, sizes, jira)),
    ("jira.waiting_time_decomposition", lambda: jira_analysis.waiting_time_decomposition(court, jira, window)),
    ("jira.carry_over", lambda: jira_analysis.carry_over(court, jira, window)),
    ("jira.point_churn", lambda: jira_analysis.point_churn(court, jira, window)),
    ("jira.worklog_cross_check", lambda: jira_analysis.worklog_cross_check(court, jira, window)),
    ("jira.conformance", lambda: jira_analysis.conformance(court, jira, window)),
    ("jira.sprints", lambda: jira_analysis.sprints(court, jira, window)),
    ("stack.metrics", lambda: stack_analysis.metrics(court, window)),
    ("rules.flow_efficiency", lambda: insights_rules.flow_efficiency(court, window)),
    ("rules.comment_tax", lambda: insights_rules.comment_tax(court, threads, window)),
    ("ballcourt.blocking_graph", lambda: analyse.blocking_graph(court, "queue", None)),
    ("ballcourt.queue_series", lambda: analyse.queue_series(court, "person", None, 24)),
    ("ballcourt.delivery_list", lambda: analyse.delivery_list(court, None)),
]:
    timed(name, fn)

t0 = time.perf_counter()
payload = ticket_view.ticket(court, jira, "DEL-167")
print(f"{'ticket(DEL-167)':<46} {(time.perf_counter()-t0)*1000:9.1f} ms   payload {len(json.dumps(payload, default=str))} bytes")

calib = timed("simulation.calibrate", lambda: policy_lab.calibrate(court, None, conn=con))
for reps in (1, 8):
    timed(f"simulation.simulate baseline reps={reps}", lambda reps=reps: policy_lab.simulate(calib, None, replications=reps))
timed("simulation.simulate scenario reps=8", lambda: policy_lab.simulate(
    calib, policy_lab.Scenario.from_dict({"wip_limit": 4}), replications=8))

if PROFILE:
    pr = cProfile.Profile()
    pr.enable()
    insights_rules.insights(court, threads, window, level="team", bots=bots, sizes=sizes, jira=jira)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(35)
    print(s.getvalue())
con.close()
