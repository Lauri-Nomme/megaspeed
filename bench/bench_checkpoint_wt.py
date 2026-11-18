"""Measure scrape-side checkpoint cost in a worktree (before/after item 1).

Runs the fixture transport through ingest.scrape.scrape with checkpoint_every
50 (the default) vs 0 (disabled) and reports wall time, bundle bytes and the
bytes each incremental checkpoint staged.
"""
import sys
import time
from pathlib import Path

WT = sys.argv[1] if len(sys.argv) > 1 else "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape"
sys.path.insert(0, WT)

from ingest import bundle as bundle_mod
from ingest import fixtures as fixtures_mod
from ingest.config import load_config
from ingest.scrape import scrape
from ingest.transport import FixtureTransport

STAGED: list[int] = []
_orig = bundle_mod.BundleSpool.append


def counting_append(self, **kwargs):
    written = _orig(self, **kwargs)
    STAGED.append(written["bytes"])
    return written


bundle_mod.BundleSpool.append = counting_append


def run(n: int, checkpoint_every: int, tag: str) -> tuple[float, dict, list[int]]:
    cfg = load_config(None)
    payload = fixtures_mod.generate(n=n, seed=20261115)
    transport = FixtureTransport(payload)
    out = Path(f"/tmp/opencode/perf/bundles/{tag}-n{n}-cp{checkpoint_every}.tgz")
    out.parent.mkdir(parents=True, exist_ok=True)
    STAGED.clear()
    t0 = time.perf_counter()
    report = scrape(
        cfg, transport, "fixture", out=out,
        state_dir=out.parent / f"state-{tag}-{n}-{checkpoint_every}",
        full=True, checkpoint_every=checkpoint_every, progress=None,
    )
    elapsed = time.perf_counter() - t0
    return elapsed, report, list(STAGED)


print(f"{'n':>6} {'cp=50 s':>9} {'cp=0 s':>9} {'overhead':>9} {'bundle KB':>10} "
      f"{'events':>8} {'ckpts':>6} {'staged KB':>10} {'max ckpt KB':>12}")
for n in (500, 1000, 2000, 4000):
    t50, r50, staged = run(n, 50, "wt")
    t0, r0, _ = run(n, 0, "wt")
    print(f"{n:>6} {t50:>9.2f} {t0:>9.2f} {t50/max(t0,1e-9):>8.1f}x "
          f"{r50['bundle_bytes']/1024:>10.0f} {r50['events']:>8} {len(staged):>6} "
          f"{sum(staged)/1024:>10.0f} {max(staged, default=0)/1024:>12.1f}")
