"""megaspeed ingest — Bitbucket Data Center -> pseudonymised event store.

    python3 -m ingest --fixture --n 50
    python3 -m ingest --verify

The raw zone starts and ends in this package: nothing here is imported by the
server, and the server never opens the identity map.
"""

__all__ = ["config", "taxonomy", "pseudonym", "fixtures", "bitbucket", "transport", "pipeline", "store"]
