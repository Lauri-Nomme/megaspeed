#!/usr/bin/env python3
"""Entry point of the self-contained ``megaspeed-scrape`` artifact.

``scrape/build.sh`` freezes this script with PyInstaller (``--onefile``) -- or,
as a stdlib fallback, packs it plus the ``ingest`` package into a zipapp. Both
artifacts run the same code:

    megaspeed-scrape --live --repo DEL/delivery-api --out export/del.tgz
    megaspeed-scrape --fixture --n 50 --out /tmp/fixture.tgz
    megaspeed-scrape --version

No secrets are baked in: credentials, ticket projects, bot slugs and repos come
from the environment or a runtime ``--config`` TOML file (prd/13).
"""

from __future__ import annotations

import sys
from pathlib import Path


def _ensure_source_tree_on_path() -> None:
    """Run from a checkout: ``python scrape/main.py`` (before/without a build)."""
    try:
        root = Path(__file__).resolve().parent.parent
    except (OSError, RuntimeError):  # frozen / zipapp: no useful __file__
        return
    if (root / "ingest" / "__init__.py").is_file() and str(root) not in sys.path:
        sys.path.insert(0, str(root))


def main(argv: list[str] | None = None) -> int:
    _ensure_source_tree_on_path()
    from ingest.scrape_cli import main as scrape_main

    return scrape_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
