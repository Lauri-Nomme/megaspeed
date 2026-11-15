"""megaspeed dev server — read-only API over the pseudonymised stores.

``server.tenants`` is the tenant registry (one ``<dir>/<tenant>.duckdb`` per
dataset, prd/12); ``server.app`` opens them read-only and serves ``app/``.
"""

__all__ = ["app", "harness", "query", "tenants"]
