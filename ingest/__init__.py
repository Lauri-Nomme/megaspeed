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

from pathlib import Path


def _version() -> str:
    """The release version: the repo's ``VERSION`` file, or the baked fallback.

    A source checkout (and the scrape artifact) must report the same version the
    server and the bundle manifest use. The hardcoded value is the last resort
    for a build that ships without ``VERSION`` (a PyInstaller onefile), and is
    kept in sync by ``tests/test_version.py``.
    """
    try:
        baked = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except OSError:
        return "0.20.7"
    return baked[1:] if baked.startswith("v") else baked


__version__ = _version()

__all__ = [
    "config", "taxonomy", "timeutil", "pseudonym", "fixtures", "bitbucket",
    "transport", "pipeline", "bundle", "scrape", "importer", "store", "live",
    "classify", "gitclass", "jira", "jira_fixtures", "jira_pipeline",
    "jira_cloud", "scrape_jira", "pepper",
]
