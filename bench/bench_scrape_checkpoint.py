"""Measure scrape-side checkpoint cost: full-bundle rewrite per checkpoint.

Runs the fixture transport through ingest.scrape.scrape with checkpoint_every
50 (the default) vs 0 (disabled) and reports wall time + bytes written.
"""
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed")

from ingest import fixtures as fixtures_mod
from ingest import scrape as scrape_mod
from ingest.config import load_config
from ingest.scrape import scrape
from ingest.transport import FixtureTransport


def run(n: int, checkpoint_every: int) -> tuple[float, dict]:
    cfg = load_config(None)
    payload = fixtures_mod.generate(n=n, seed=20261115)
    transport = FixtureTransport(payload)
    out = Path(f"/tmp/opencode/perf/bundles/n{n}-cp{checkpoint_every}.tgz")
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    report = scrape(
        cfg, transport, "fixture", out=out, state_dir=out.parent / f"state-{n}-{checkpoint_every}",
        full=True, checkpoint_every=checkpoint_every,
        progress=None,
    )
    elapsed = time.perf_counter() - t0
    return elapsed, report


print(f"{'n':>6} {'cp=50 s':>9} {'cp=0 s':>9} {'overhead':>9} {'bundle KB':>10} {'events':>8}")
for n in (500, 1000, 2000, 4000):
    t50, r50 = run(n, 50)
    t0, r0 = run(n, 0)
    print(f"{n:>6} {t50:>9.2f} {t0:>9.2f} {t50/max(t0,1e-9):>8.1f}x "
          f"{r50['bundle_bytes']/1024:>10.0f} {r50['events']:>8}")
