"""Ingest configuration: file + environment, with environment winning.

Nothing secret is committed. Credentials come from the environment only
(``MEGASPEED_BB_TOKEN`` or ``MEGASPEED_BB_USER`` / ``MEGASPEED_BB_PASSWORD``)
because they live in the raw zone.
"""

from __future__ import annotations

import dataclasses
import os
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "ingest" / "config.toml"

DEFAULT_TICKET_REGEX = r"\b[A-Z]+-\d+\b"
# A defensive second gate: whatever the configurable regex matches must still
# look like a ticket key before it may enter the store. This stops a sloppy
# regex from smuggling prose into the keys column.
DEFAULT_KEY_SHAPE = r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$"

# The accepted *project prefixes*. A key only counts when its prefix is on this
# list, so technical tokens that happen to look like keys (``UTF-8``,
# ``SHA-256``, ``ISO-8601``) never become tickets. Configure the real projects:
#   MEGASPEED_TICKET_PROJECTS=DEL,FIX  (or [filter].ticket_projects)
# The default is the fixture/test project set; a live run should set it.
DEFAULT_TICKET_PROJECTS = ("PROJ", "OPS", "ARCH")
_PROJECT_PREFIX = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Known non-human accounts (prd/02 "Bot vs human": deterministic, known account
# set). DC's user type SERVICE is always a bot; these slugs catch CI/build
# accounts that DC reports as NORMAL users.
DEFAULT_BOT_SLUGS = (
    "bitbucket.system-user",
    "stash.system-user",
    "jenkins",
    "bamboo",
    "github-actions",
    "gitlab-runner",
)


@dataclasses.dataclass
class Config:
    db_path: Path
    identity_path: Path
    ticket_regex: str
    key_shape: str
    repos: list[tuple[str, str]]
    ticket_projects: tuple[str, ...] = DEFAULT_TICKET_PROJECTS
    bot_slugs: tuple[str, ...] = DEFAULT_BOT_SLUGS
    base_url: str = ""
    page_limit: int = 25
    seed: int = 20261115
    fixture_path: Path | None = None
    sentinel_slugs: tuple[str, ...] = ()
    auth_user: str = ""
    auth_token: str = ""
    auth_password: str = ""

    @property
    def live(self) -> bool:
        return bool(self.base_url)

    @property
    def has_credentials(self) -> bool:
        return bool(self.auth_token or (self.auth_user and self.auth_password))


def _resolve(value: str | os.PathLike[str], base: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def parse_projects(value: str | list | tuple) -> tuple[str, ...]:
    """Parse a project-prefix list (``DEL,FIX`` or ``["DEL", "FIX"]``)."""
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    projects: list[str] = []
    for item in items:
        prefix = str(item).strip().upper()
        if not prefix:
            continue
        if not _PROJECT_PREFIX.fullmatch(prefix):
            raise SystemExit(
                f"bad ticket project prefix {prefix!r}: expected [A-Z][A-Z0-9_]* "
                "(MEGASPEED_TICKET_PROJECTS=DEL,FIX)"
            )
        if prefix not in projects:
            projects.append(prefix)
    if not projects:
        raise SystemExit(
            "no ticket project prefixes configured: set MEGASPEED_TICKET_PROJECTS=DEL,FIX "
            "(a bare key-shaped token like UTF-8 must never count as a ticket)"
        )
    return tuple(projects)


def _parse_slugs(value: str | list | tuple) -> tuple[str, ...]:
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return tuple(dict.fromkeys(str(i).strip().lower() for i in items if str(i).strip()))


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
    actors = raw.get("actors", {})

    repos = [(str(r["project"]), str(r["repo"])) for r in source.get("repos", [])]

    fixture_path = os.environ.get("MEGASPEED_FIXTURE_PATH", fixture.get("path") or "")
    config = Config(
        db_path=_resolve(os.environ.get("MEGASPEED_DB", store.get("db", "data/megaspeed.duckdb")), base),
        identity_path=_resolve(
            os.environ.get("MEGASPEED_IDENTITY", store.get("identity", "var/identity_map.json")), base
        ),
        ticket_regex=os.environ.get("MEGASPEED_TICKET_REGEX", filt.get("ticket_regex", DEFAULT_TICKET_REGEX)),
        key_shape=os.environ.get("MEGASPEED_KEY_SHAPE", filt.get("key_shape", DEFAULT_KEY_SHAPE)),
        ticket_projects=parse_projects(
            os.environ.get("MEGASPEED_TICKET_PROJECTS", filt.get("ticket_projects", list(DEFAULT_TICKET_PROJECTS)))
        ),
        bot_slugs=_parse_slugs(
            os.environ.get("MEGASPEED_BOT_SLUGS", actors.get("bot_slugs", list(DEFAULT_BOT_SLUGS)))
        ),
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
        auth_token=os.environ.get("MEGASPEED_BB_TOKEN", ""),
        auth_password=os.environ.get("MEGASPEED_BB_PASSWORD", ""),
    )
    if not config.repos:
        raise SystemExit(f"no repos configured (see {cfg_path}: [[source.repos]])")
    if config.base_url and not config.has_credentials:
        raise SystemExit(
            "MEGASPEED_BB_BASE_URL is set but no credentials: set MEGASPEED_BB_TOKEN"
            " or MEGASPEED_BB_USER + MEGASPEED_BB_PASSWORD"
        )
    return config
