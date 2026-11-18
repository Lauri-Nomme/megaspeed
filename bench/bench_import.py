"""Measure the import/store layer: gate, write, derive, verify — and re-import.

Uses the checkpoint-free bundles produced by bench_scrape_checkpoint.py.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, "/vokk/home/lauri/dev/megaspeed")

from ingest import bundle as bundle_mod
from ingest import importer
from ingest.config import load_config

BUNDLES = Path("/tmp/opencode/perf/bundles")


def ms(t0):
    return (time.perf_counter() - t0) * 1000


print(f"{'n':>6} {'bundle MB':>10} {'read':>8} {'gate':>8} {'import':>9} {'re-import':>10} {'verify':>8} {'events':>8}")
for n in (500, 1000, 2000, 4000):
    path = BUNDLES / f"n{n}-cp0.tgz"
    t = time.perf_counter()
    b = bundle_mod.read_bundle(path)
    read_ms = ms(t)
    cfg = load_config(None)
    cfg.db_path = Path(f"/tmp/opencode/perf/stores/imp{n}.duckdb")
    cfg.db_path.unlink(missing_ok=True)
    t = time.perf_counter()
    importer.validate_bundle(b, key_shape=cfg.key_shape, ticket_projects=cfg.ticket_projects,
                             ticket_keys=cfg.ticket_keys)
    gate_ms = ms(t)
    t = time.perf_counter()
    r1 = importer.import_bundle(cfg, path)
    import_ms = ms(t)
    t = time.perf_counter()
    r2 = importer.import_bundle(cfg, path)
    reimport_ms = ms(t)
    import duckdb as _d
    con = _d.connect(str(cfg.db_path), read_only=True)
    t = time.perf_counter()
    from ingest.store import verify
    verify(con, cfg.key_shape, cfg.ticket_projects, cfg.ticket_keys)
    verify_ms = ms(t)
    con.close()
    print(f"{n:>6} {path.stat().st_size/1e6:>10.1f} {read_ms:>7.0f}ms {gate_ms:>7.0f}ms "
          f"{import_ms:>8.0f}ms {reimport_ms:>9.0f}ms {verify_ms:>7.0f}ms {r1['events_loaded']:>8}")
