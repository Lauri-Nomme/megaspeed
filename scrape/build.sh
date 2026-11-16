#!/usr/bin/env bash
# Build the self-contained scraper artifact: scrape/dist/megaspeed-scrape
#
#   scrape/build.sh                 # PyInstaller --onefile (falls back to zipapp)
#   scrape/build.sh --zipapp        # force the stdlib zipapp fallback
#   scrape/build.sh --pyinstaller   # force PyInstaller (fail if unavailable)
#   scrape/build.sh --python PREFIX/bin/python   # pick the interpreter
#   scrape/build.sh --no-smoke      # skip the post-build bundle smoke test
#
# The artifact bundles Python + the stdlib-only scrape side (source -> classify
# -> pseudonymise -> tgz). It does NOT bundle DuckDB, the server, the local LLM
# (that is a provisioned localhost service) or any credential (prd/13).
#
# Must work locally exactly as CI does; the workflow calls this script.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRAPE_DIR="$ROOT/scrape"
DIST_DIR="$SCRAPE_DIR/dist"
WORK_DIR="$SCRAPE_DIR/build"
NAME="megaspeed-scrape"
MODE="auto"
SMOKE=1
PYTHON="${PYTHON:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --zipapp) MODE="zipapp" ;;
    --pyinstaller) MODE="pyinstaller" ;;
    --mode) MODE="${2:?--mode needs auto|pyinstaller|zipapp}"; shift ;;
    --python) PYTHON="${2:?--python needs a path}"; shift ;;
    --no-smoke) SMOKE=0 ;;
    -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "build.sh: unknown argument $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

case "$MODE" in
  auto|pyinstaller|zipapp) ;;
  *) echo "build.sh: bad mode $MODE (auto|pyinstaller|zipapp)" >&2; exit 2 ;;
esac

# Interpreter: --python, else $PYTHON, else the repo venv, else python3.
if [ -z "$PYTHON" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PYTHON="$ROOT/.venv/bin/python"; else PYTHON="python3"; fi
fi
"$PYTHON" - <<'EOF'
import sys
if sys.version_info < (3, 11):
    raise SystemExit(f"build.sh: python >= 3.11 required (tomllib), found {sys.version.split()[0]}")
EOF

have_pyinstaller() { "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; }

clean() {
  rm -rf "$WORK_DIR"
  mkdir -p "$DIST_DIR"
  rm -f "$DIST_DIR/$NAME"
}

build_pyinstaller() {
  echo "build: PyInstaller --onefile ($("$PYTHON" -c 'import PyInstaller; print(PyInstaller.__version__)'))"
  "$PYTHON" -m PyInstaller \
    --onefile \
    --clean \
    --noconfirm \
    --noupx \
    --log-level WARN \
    --name "$NAME" \
    --paths "$ROOT" \
    --distpath "$DIST_DIR" \
    --workpath "$WORK_DIR/pyinstaller" \
    --specpath "$WORK_DIR" \
    --exclude-module duckdb \
    --exclude-module fastapi \
    --exclude-module uvicorn \
    --exclude-module websockets \
    --exclude-module playwright \
    --exclude-module tkinter \
    "$SCRAPE_DIR/main.py"
}

build_zipapp() {
  echo "build: zipapp (stdlib fallback)"
  local stage="$WORK_DIR/zipapp"
  mkdir -p "$stage"
  cp "$SCRAPE_DIR/main.py" "$stage/__main__.py"
  mkdir -p "$stage/ingest"
  # Only the scrape side ships. The import/store modules (and the dev-shortcut
  # `live` wrapper that pulls them in) are left out: the artifact's job is
  # source -> classify -> pseudonymise -> tgz, nothing else.
  (cd "$ROOT" && find ingest -maxdepth 1 -name '*.py' \
     ! -name 'store.py' ! -name 'importer.py' ! -name 'live.py' ! -name '__main__.py' \
     -print0 | xargs -0 -I{} cp {} "$stage/ingest/")
  "$PYTHON" -m zipapp "$stage" \
    --python "/usr/bin/env python3" \
    --output "$DIST_DIR/$NAME"
}

smoke() {
  local artifact="$DIST_DIR/$NAME"
  chmod +x "$artifact"
  echo "build: smoke test"
  "$artifact" --version
  local tmp
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' RETURN
  # A real (tiny) scrape, proving the artifact runs with no repo, no venv and
  # no installed dependencies: fixture source -> tgz on disk. Repos come from
  # the environment, exactly as they will on the airgapped box.
  MEGASPEED_REPOS="ENG/platform,ENG/mobile" \
    "$artifact" --fixture --n 2 --out "$tmp/smoke.tgz" --json >"$tmp/smoke.json"
  "$PYTHON" - "$tmp/smoke.json" <<'EOF'
import json, sys, tarfile
report = json.load(open(sys.argv[1]))
assert report["events"] > 0, report
with tarfile.open(report["bundle"], "r:gz") as tar:
    names = sorted(tar.getnames())
assert names == ["export/events.jsonl", "export/manifest.json", "export/objects.jsonl", "export/sha256sums"], names
print(f"  ok: {report['events']} events / {report['objects']} objects from {report['prs_kept']} PRs")
EOF
  # The Jira half is part of the same artifact: fixture source -> tgz, no
  # credentials, no Bitbucket repos. The fixture's project is a default ticket
  # project, so this needs no environment at all.
  "$artifact" --jira --fixture --out "$tmp/jira.tgz" --json >"$tmp/jira.json"
  "$PYTHON" - "$tmp/jira.json" <<'EOF'
import json, sys, tarfile
report = json.load(open(sys.argv[1]))
assert report["events"] > 0, report
assert report["counts"]["stories"] > 0, report
assert report["counts"]["sprints"] > 0, report
assert report["worklogs"] > 0, report
with tarfile.open(report["bundle"], "r:gz") as tar:
    names = sorted(tar.getnames())
assert names == ["export/events.jsonl", "export/manifest.json", "export/objects.jsonl", "export/sha256sums"], names
print(f"  ok: {report['events']} jira events / {report['objects']} objects"
      f" ({report['counts']['epics']} epic, {report['counts']['stories']} stories,"
      f" {report['worklogs']} worklogs)")
EOF
  ls -l "$artifact"
}

clean
if [ "$MODE" = "pyinstaller" ]; then
  have_pyinstaller || { echo "build.sh: PyInstaller not installed ($PYTHON -m pip install -r scrape/requirements-build.txt)" >&2; exit 1; }
  build_pyinstaller
elif [ "$MODE" = "auto" ] && have_pyinstaller; then
  build_pyinstaller
else
  if [ "$MODE" = "auto" ]; then
    echo "build: PyInstaller not available, using the zipapp fallback"
  fi
  build_zipapp
fi

[ -f "$DIST_DIR/$NAME" ] || { echo "build.sh: no artifact at $DIST_DIR/$NAME" >&2; exit 1; }
if [ "$SMOKE" = "1" ]; then
  smoke
fi

echo "build: artifact $DIST_DIR/$NAME"
