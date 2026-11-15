"""Ingest configuration: file + environment, with environment winning.

Nothing secret is committed. Credentials come from the environment only
(``MEGASPEED_BB_TOKEN`` or ``MEGASPEED_BB_USER`` / ``MEGASPEED_BB_PASSWORD``)
because they live in the raw zone.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "ingest" / "config.toml"

DEFAULT_TICKET_REGEX = r"\b[A-Z]+-\d+\b"
# A defensive second gate: whatever the configurable regex matches must still
# look like a ticket key before it may enter the store. This stops a sloppy
# regex from smuggling prose into the keys column.
DEFAULT_KEY_SHAPE = r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$"


@dataclasses.dataclass
class Config:
    db_path: Path
    identity_path: Path
    ticket_regex: str
    key_shape: str
    repos: list[tuple[str, str]]
    base_url: str = ""
    page_limit: int = 25
    seed: int = 20261115
    fixture_path: Path | None = None
    sentinel_slugs: tuple[str, ...] = ()
    auth_user: str = ""
    auth_secret: str = ""

    @property
    def live(self) -> bool:
        return bool(self.base_url)


def _resolve(value: str | os.PathLike[str], base: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    cfg_path = Path(path).expanduser() if path else Path(
        os.environ.get("MEGASPEED_CONFIG", DEFAULT_CONFIG_PATH)
    )
    raw: dict = {}
    if cfg_path.exists():
        raw = tomllib.loads(cfg_path.read_text())
    base = cfg_path.resolve().parent.parent if cfg_path.exists() else REPO_ROOT

    store = raw.get("store", {})
    filt = raw.get("filter", {})
    source = raw.get("source", {})
    bb = raw.get("bitbucket", {})
    fixture = raw.get("fixture", {})
    review = raw.get("review", {})

    repos = [(str(r["project"]), str(r["repo"])) for r in source.get("repos", [])]

    fixture_path = os.environ.get("MEGASPEED_FIXTURE_PATH", fixture.get("path") or "")
    config = Config(
        db_path=_resolve(os.environ.get("MEGASPEED_DB", store.get("db", "data/megaspeed.duckdb")), base),
        identity_path=_resolve(
            os.environ.get("MEGASPEED_IDENTITY", store.get("identity", "var/identity_map.json")), base
        ),
        ticket_regex=os.environ.get("MEGASPEED_TICKET_REGEX", filt.get("ticket_regex", DEFAULT_TICKET_REGEX)),
        key_shape=os.environ.get("MEGASPEED_KEY_SHAPE", filt.get("key_shape", DEFAULT_KEY_SHAPE)),
        repos=repos,
        base_url=os.environ.get("MEGASPEED_BB_BASE_URL", bb.get("base_url", "")).rstrip("/"),
        page_limit=int(os.environ.get("MEGASPEED_PAGE_LIMIT", bb.get("page_limit", 25))),
        seed=int(os.environ.get("MEGASPEED_SEED", fixture.get("seed", 20261115))),
        fixture_path=_resolve(fixture_path, base) if fixture_path else None,
        sentinel_slugs=tuple(
            os.environ.get("MEGASPEED_SENTINEL_SLUGS", ",".join(review.get("sentinel_slugs", [])))
            .replace(" ", "")
            .split(",")
        )
        if review.get("sentinel_slugs") or os.environ.get("MEGASPEED_SENTINEL_SLUGS")
        else (),
        auth_user=os.environ.get("MEGASPEED_BB_USER", ""),
        auth_secret=os.environ.get("MEGASPEED_BB_TOKEN", "") or os.environ.get("MEGASPEED_BB_PASSWORD", ""),
    )
    if not config.repos:
        raise SystemExit(f"no repos configured (see {cfg_path}: [[source.repos]])")
    if config.base_url and not config.auth_secret:
        raise SystemExit("MEGASPEED_BB_BASE_URL is set but no MEGASPEED_BB_TOKEN / MEGASPEED_BB_PASSWORD")
    return config
