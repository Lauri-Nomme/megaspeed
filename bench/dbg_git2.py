import sys, copy, shutil
from pathlib import Path
sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed-wt/perf-scrape")
sys.path.insert(0, "/tmp/opencode/perf")
import bench_gitclass_wt as B
from ingest import gitclass

records = copy.deepcopy(B.pr_records(3))
state = B.ROOT / "cache-dbg2"
shutil.rmtree(state, ignore_errors=True)
clf = gitclass.PushClassifier(state, f"file://{B.ROOT}/remote",
                              url_template="{base}/{project}/{repo}.git")
orig = gitclass.PushClassifier._run
def traced(self, path, args, **kw):
    r = orig(self, path, args, **kw)
    if args[0] == "fetch":
        print("  FETCH", args, "->", r.code)
    return r
gitclass.PushClassifier._run = traced
for i, (pr, acts, commits) in enumerate(records):
    clf.annotate("ENG", "platform", pr, acts)
    clf.annotate_diff_size("ENG", "platform", pr, commits)
    print("PR", i, "runs", clf.runs, "fetches", clf.fetches)
