"""megaspeed ingest — Bitbucket DC + Jira (DC / Cloud) -> pseudonymised store.

Two sides, one airgap (prd/11):

    python3 -m ingest scrape --live --repo DEL/delivery-api --out export/del.tgz
    python3 -m ingest scrape --jira --live --project DEL --out export/del-jira.tgz
    python3 -m ingest scrape --jira-cloud --live --project DEL --out export/del-cloud.tgz
    python3 -m ingest import export/del.tgz --db data/del.duckdb

    python3 -m ingest --fixture --n 50      # dev shortcut: scrape+import in-process
    python3 -m ingest --jira --fixture      # the same for the Jira side
    python3 -m ingest --verify

Two sources, one store: Bitbucket gives the PR layer (PR / PRRevision /
ReviewThread / Commit), Jira gives the ticket layer (Epic / Story / Sprint +
worklogs), and the two join on the ticket key (prd/03, prd/04).

The raw zone starts and ends in the scrape side: nothing here is imported by the
server, and the server never opens the identity map. The scrape side
(``scrape``, ``scrape_jira``, ``bundle``, ``pipeline``, ``jira``,
``jira_cloud``, ``jira_pipeline``, ``pseudonym``, ...) is deliberately
importable without DuckDB -- that is what makes the ``megaspeed-scrape``
artifact stdlib-sized (prd/13).
"""

__version__ = "0.2.0"

__all__ = [
    "config", "taxonomy", "timeutil", "pseudonym", "fixtures", "bitbucket",
    "transport", "pipeline", "bundle", "scrape", "importer", "store", "live",
    "classify", "gitclass", "jira", "jira_fixtures", "jira_pipeline",
    "jira_cloud", "scrape_jira", "pepper",
]
