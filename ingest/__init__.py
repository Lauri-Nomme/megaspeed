"""megaspeed ingest — Bitbucket Data Center -> pseudonymised event store.

Two sides, one airgap (prd/11):

    python3 -m ingest scrape --live --repo DEL/delivery-api --out export/del.tgz
    python3 -m ingest import export/del.tgz --db data/del.duckdb

    python3 -m ingest --fixture --n 50      # dev shortcut: scrape+import in-process
    python3 -m ingest --verify

The raw zone starts and ends in the scrape side: nothing here is imported by the
server, and the server never opens the identity map. The scrape side
(``scrape``, ``bundle``, ``pipeline``, ``pseudonym``, ...) is deliberately
importable without DuckDB -- that is what makes the ``megaspeed-scrape``
artifact stdlib-sized (prd/13).
"""

__version__ = "0.2.0"

__all__ = [
    "config", "taxonomy", "timeutil", "pseudonym", "fixtures", "bitbucket",
    "transport", "pipeline", "bundle", "scrape", "importer", "store", "live",
]
