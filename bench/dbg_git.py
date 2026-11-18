import sys, copy
from pathlib import Path
sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape")
sys.path.insert(0, "/tmp/opencode/perf")
import bench_gitclass_wt as B
from ingest import gitclass

B.build_remote()
records = copy.deepcopy(B.pr_records(5))
clf = gitclass.PushClassifier(B.ROOT / "cache-dbg", f"file://{B.ROOT}/remote",
                              url_template="{base}/{project}/{repo}.git")
import shutil
shutil.rmtree(B.ROOT / "cache-dbg", ignore_errors=True)
orig = gitclass.PushClassifier._run
def traced(self, path, args, **kw):
    r = orig(self, path, args, **kw)
    print("  run", args[0], args[1:3], "->", r.code)
    return r
gitclass.PushClassifier._run = traced
for i, (pr, acts, commits) in enumerate(records):
    print("PR", i, "wants", acts[0]["previousFromHash"][:7], acts[0]["fromHash"][:7])
    clf.annotate("ENG", "platform", pr, acts)
    clf.annotate_diff_size("ENG", "platform", pr, commits)
    print("   runs", clf.runs, "fetches", clf.fetches)
