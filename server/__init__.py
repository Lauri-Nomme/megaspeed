"""megaspeed dev server — read-only API over the pseudonymised stores.

``server.tenants`` is the tenant registry (one ``<dir>/<tenant>.duckdb`` per
dataset, prd/12); ``server.app`` opens them read-only and serves ``app/``;
``server.version`` resolves the build identity (prd/32).
"""

from server.version import __version__  # noqa: F401  (prd/32: the app version)

__all__ = ["__version__", "app", "harness", "query", "tenants", "version"]
