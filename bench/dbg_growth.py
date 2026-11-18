import io, sys
from pathlib import Path
sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape")
sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape/scrape/tests")
from unittest import mock
from ingest import bundle as bundle_mod
import test_checkpoint_incremental as T
from ingest.scrape import scrape
from ingest.progress import ProgressReporter

import tempfile
tmp = tempfile.TemporaryDirectory()
root = Path(tmp.name)
cfg = T._cfg(root)
out = root / "export" / "huge.tgz"
staged = []
orig = bundle_mod.BundleSpool.append
def counting(spool, **kw):
    w = orig(spool, **kw)
    staged.append((w["events"], w["objects"], w["bytes"]))
    return w
with mock.patch.object(bundle_mod.BundleSpool, "append", counting):
    scrape(cfg, T.Harness(T._bundle()), "fixture", out=out, state_dir=root/"export"/"s",
           overlap_hours=T.NO_CUTOFF, progress=ProgressReporter(stream=io.StringIO(), interval=0),
           checkpoint_every=1)
print("first 6:", staged[:6])
print("last 3:", staged[-3:])
print("totals:", sum(s[0] for s in staged), sum(s[1] for s in staged))
