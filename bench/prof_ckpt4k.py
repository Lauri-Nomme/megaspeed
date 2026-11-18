import cProfile, pstats, sys, io
from pathlib import Path
sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape")
from ingest import fixtures as fixtures_mod
from ingest.config import load_config
from ingest.scrape import scrape
from ingest.transport import FixtureTransport

n = 4000
cfg = load_config(None)
payload = fixtures_mod.generate(n=n, seed=20261115)
out = Path("/tmp/opencode/perf/bundles/prof4k.tgz")
def go():
    scrape(cfg, FixtureTransport(payload), "fixture", out=out,
           state_dir=out.parent / "state-prof4k", full=True, checkpoint_every=50, progress=None)
pr = cProfile.Profile(); pr.enable(); go(); pr.disable()
s = io.StringIO(); pstats.Stats(pr, stream=s).sort_stats("tottime").print_stats(18)
print(s.getvalue())
