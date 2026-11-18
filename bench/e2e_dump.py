import sys, json
from pathlib import Path
WT = sys.argv[1]
tag = sys.argv[2]
sys.path.insert(0, WT)
from ingest import fixtures
from ingest.config import load_config
from ingest.importer import import_bundle
from ingest.scrape import scrape
from ingest.store import connect
from ingest.transport import FixtureTransport

root = Path(f"/tmp/opencode/e2e-{tag}")
import shutil; shutil.rmtree(root, ignore_errors=True); root.mkdir(parents=True)
cfg = load_config(None)
cfg.db_path = root / "store.duckdb"
cfg.identity_path = root / "identity.json"
payload = fixtures.generate(n=120, seed=7)
report = scrape(cfg, FixtureTransport(payload), "fixture", out=root / "e.tgz",
                checkpoint_every=7, full=True)
import_bundle(cfg, root / "e.tgz")
con = connect(cfg.db_path, read_only=True)
events = con.execute("SELECT event_key, activity, actor, repo, pr_id, CAST(attributes AS VARCHAR), classifier_version, content_hash FROM events ORDER BY event_key").fetchall()
objects = con.execute("SELECT object_type, object_id, repo, pr_id, actor, state, CAST(ticket_keys AS VARCHAR), CAST(attributes AS VARCHAR) FROM objects ORDER BY object_type, object_id").fetchall()
rels = con.execute("SELECT event_key, object_type, object_id FROM event_objects ORDER BY 1,2,3").fetchall()
con.close()
Path(f"/tmp/opencode/perf/e2e-{tag}.json").write_text(json.dumps(
    {"events": events, "objects": objects, "rels": rels}, default=str))
print(tag, "events", len(events), "objects", len(objects), "rels", len(rels),
      "report events", report["events"], "objects", report["objects"])
