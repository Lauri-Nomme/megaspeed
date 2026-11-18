"""Per-PR git subprocess breakdown for the push_class/diff-size cache."""
import collections
import copy
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed")
from ingest import gitclass

ROOT = Path("/tmp/opencode/perf/git")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 50
sys.path.insert(0, "/tmp/opencode/perf")
import bench_gitclass as B  # reuse the built remote + records

B.build_remote()
records = copy.deepcopy(B.pr_records(N))
state = ROOT / "cache2"
import shutil
if state.exists():
    shutil.rmtree(state)

counter = collections.Counter()
orig = gitclass.PushClassifier._run


def counting(self, path, args, **kw):
    counter[args[0]] += 1
    return orig(self, path, args, **kw)


gitclass.PushClassifier._run = counting
clf = gitclass.PushClassifier(state, f"file://{ROOT}/remote", url_template="{base}/{project}/{repo}.git")
t0 = time.perf_counter()
for pr, acts, commits in records:
    clf.annotate("ENG", "platform", pr, acts)
    clf.annotate_diff_size("ENG", "platform", pr, commits)
el = time.perf_counter() - t0
total = sum(counter.values())
print(f"n={N}: {el:.2f}s, {el/N*1000:.1f} ms/PR, {total} git runs = {total/N:.1f}/PR")
for name, count in counter.most_common():
    print(f"  {name:<12} {count:6d}  {count/N:6.1f}/PR")
