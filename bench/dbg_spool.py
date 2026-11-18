import sys, time
from pathlib import Path
sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape")
from ingest import bundle as bundle_mod
from ingest import fixtures as fixtures_mod
from ingest.config import load_config
from ingest.scrape import scrape
from ingest.transport import FixtureTransport

stats = []
orig = bundle_mod.BundleSpool.append
def counting(self, **kw):
    r = orig(self, **kw)
    stats.append((r["events"], r["objects"], r["bytes"]))
    return r
bundle_mod.BundleSpool.append = counting

n = 2000
cfg = load_config(None)
payload = fixtures_mod.generate(n=n, seed=20261115)
out = Path(f"/tmp/opencode/perf/bundles/dbg.tgz")
report = scrape(cfg, FixtureTransport(payload), "fixture", out=out,
                state_dir=out.parent / "state-dbg", full=True, checkpoint_every=50, progress=None)
print("events", report["events"], "objects", report["objects"])
print("staged events", sum(s[0] for s in stats), "objects", sum(s[1] for s in stats),
      "bytes", sum(s[2] for s in stats))
print("first 5", stats[:5])
print("last 3", stats[-3:])
