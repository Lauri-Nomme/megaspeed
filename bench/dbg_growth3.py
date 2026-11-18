import io, sys, tempfile
from pathlib import Path
sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape")
sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape/scrape/tests")
from unittest import mock
from ingest import bundle as bundle_mod
import test_checkpoint_incremental as T
from ingest.scrape import scrape
from ingest.progress import ProgressReporter

tmp = tempfile.TemporaryDirectory()
root = Path(tmp.name)
cfg = T._cfg(root)
out = root / "export" / "huge.tgz"
orig = bundle_mod.BundleSpool.append
calls = []
def counting(spool, **kw):
    known = dict(spool._objects)
    fresh = []
    for r in kw["objects"]:
        k = r["object_id"]
        if known.get(k) is r:
            continue
        if known.get(k) == r:
            continue
        fresh.append(k)
        known[k] = r
    calls.append(fresh)
    return orig(spool, **kw)
with mock.patch.object(bundle_mod.BundleSpool, "append", counting):
    scrape(cfg, T.Harness(T._bundle()), "fixture", out=out, state_dir=root/"export"/"s",
           overlap_hours=T.NO_CUTOFF, progress=ProgressReporter(stream=io.StringIO(), interval=0),
           checkpoint_every=1)
for i, fresh in enumerate(calls[:8]):
    print(i, fresh)
print("last", calls[-2])
