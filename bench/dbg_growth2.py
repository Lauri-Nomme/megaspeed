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
orig_objects = bundle_mod.BundleSpool.append
calls = []
def counting(spool, **kw):
    known = set(spool._objects)
    fresh = [r["object_id"] for r in kw["objects"] if r["object_id"] not in known]
    calls.append(fresh)
    return orig_objects(spool, **kw)
with mock.patch.object(bundle_mod.BundleSpool, "append", counting):
    scrape(cfg, T.Harness(T._bundle()), "fixture", out=out, state_dir=root/"export"/"s",
           overlap_hours=T.NO_CUTOFF, progress=ProgressReporter(stream=io.StringIO(), interval=0),
           checkpoint_every=1)
for i, fresh in enumerate(calls[:6]):
    print(i, fresh)
print("...")
for i, fresh in enumerate(calls[-3:]):
    print(len(calls)-3+i, fresh[:8], "...", len(fresh))
