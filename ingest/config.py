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

from . import classify
from .progress import DEFAULT_INTERVAL as DEFAULT_PROGRESS_INTERVAL

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

# An explicit *ticket-key set* (prd/18). When non-empty it replaces the prefix
# allowlist entirely: only these exact keys count, which is what a shared Jira
# project needs (a prefix matches everyone's tickets). Configure it as
#   MEGASPEED_TICKET_KEYS=DEL-1234,DEL-5678
#   MEGASPEED_TICKET_KEYS_FILE=var/del-tickets.txt   (one key per line)
# Both sources are unioned; a malformed entry is an error, never a silent skip.
DEFAULT_TICKET_KEYS: tuple[str, ...] = ()
_KEY_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,9}-\d{1,6}$")

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
    #: An explicit ticket-key set (prd/18). Non-empty => the prefix list above is
    #: ignored and only these exact keys match.
    ticket_keys: tuple[str, ...] = DEFAULT_TICKET_KEYS
    bot_slugs: tuple[str, ...] = DEFAULT_BOT_SLUGS
    base_url: str = ""
    page_limit: int = 25
    seed: int = 20261115
    fixture_path: Path | None = None
    sentinel_slugs: tuple[str, ...] = ()
    auth_user: str = ""
    auth_token: str = ""
    auth_password: str = ""
    #: Filter on the cheap PR-list fields (title + source branch + description)
    #: before paying for any per-PR request (prd/14 §1). Off = the old "fetch
    #: commits for every PR, then filter" path, kept as an escape hatch for a
    #: source whose conventions differ (``MEGASPEED_PREFILTER=0`` /
    #: ``--no-prefilter``).
    prefilter: bool = True
    #: Seconds between progress lines on stderr; 0 disables (prd/14).
    progress_interval: float = 30.0
    #: Deterministic ``push_class`` from a local shallow git cache (prd/16).
    #: Live runs default to on; fixture bundles already carry their labels.
    push_class: bool = True
    #: Commits per fetched head, and the deepening ceiling for the merge base.
    git_depth: int = 128
    git_max_depth: int = 1024
    #: Changed lines in ``git range-diff`` at or below which a changed patch is
    #: read as a resolved conflict rather than real rework (prd/16).
    push_conflict_lines: int = 20
    #: Scrape-side text classification (prd/16): comment_type / thread_outcome /
    #: question events from the raw comment + description text before it is
    #: discarded. On by default; the local LLM is a provisioned localhost
    #: service, so a run without one degrades to the deterministic mock.
    text_classify: bool = True
    #: ``ollama`` (default for live runs) or ``mock`` (deterministic, no model).
    text_backend: str = ""
    #: The local Ollama box on the precision network (prd/16). No key: it is a
    #: local service on the raw side, never reachable from the analysis zone.
    llm_url: str = classify.DEFAULT_URL
    llm_model: str = classify.DEFAULT_MODEL
    llm_timeout: float = classify.DEFAULT_TIMEOUT
    # -- Jira source (prd/04) ---------------------------------------------
    #: Jira DC base URL, e.g. http://127.0.0.1:8090. Credentials never live in
    #: the config file: MEGASPEED_JIRA_TOKEN or _USER/_PASSWORD.
    jira_base_url: str = ""
    #: Jira projects to read. Empty => the ticket projects (the ingest is
    #: ticket-scoped, so it reads exactly the projects it accepts keys from).
    jira_projects: tuple[str, ...] = ()
    jira_auth_user: str = ""
    jira_auth_password: str = ""
    jira_auth_token: str = ""
    jira_page_limit: int = 50
    #: Politeness delay between Jira requests (seconds). 0 for a lab run.
    jira_min_interval: float = 0.2
    #: Scrum board to read sprints from; 0 = discover the project's scrum board.
    jira_board: int = 0
    #: Custom-field ids, pinned when the installation's discovery is ambiguous.
    #: Empty => discover by name ("Epic Link" / "Sprint" / "Story Points").
    jira_epic_link_field: str = ""
    jira_sprint_field: str = ""
    jira_story_points_field: str = ""

    @property
    def live(self) -> bool:
        return bool(self.base_url)

    @property
    def has_credentials(self) -> bool:
        return bool(self.auth_token or (self.auth_user and self.auth_password))

    @property
    def jira_live(self) -> bool:
        return bool(self.jira_base_url)

    @property
    def has_jira_credentials(self) -> bool:
        return bool(
            self.jira_auth_token or (self.jira_auth_user and self.jira_auth_password)
        )


def _resolve(value: str | os.PathLike[str], base: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base / p)


def parse_projects(value: str | list | tuple, *, required: bool = True) -> tuple[str, ...]:
    """Parse a project-prefix list (``DEL,FIX`` or ``["DEL", "FIX"]``).

    ``required=False`` allows an empty list: a run scoped to an explicit ticket
    set (prd/18) ignores the prefixes, so it does not need any.
    """
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
    if not projects and required:
        raise SystemExit(
            "no ticket project prefixes configured: set MEGASPEED_TICKET_PROJECTS=DEL,FIX "
            "(a bare key-shaped token like UTF-8 must never count as a ticket)"
        )
    return tuple(projects)


def parse_ticket_keys(value: str | list | tuple) -> tuple[str, ...]:
    """Parse an explicit ticket-key set (``DEL-100,DEL-101`` or a list).

    Keys are matched as whole keys, so the list is normalised to upper case (the
    same convention as the prefix list) and de-duplicated. A malformed entry is
    an operator error: silently ignoring it would scope the run to a *smaller*
    set than intended, which is the one failure mode that must not be quiet.
    """
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    keys: list[str] = []
    for item in items:
        key = str(item).strip().upper()
        if not key:
            continue
        if not _KEY_TOKEN.fullmatch(key):
            raise SystemExit(
                f"bad ticket key {key!r}: expected PROJECT-123 "
                "(MEGASPEED_TICKET_KEYS=DEL-100,DEL-101)"
            )
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def load_ticket_keys_file(path: str | os.PathLike[str]) -> tuple[str, ...]:
    """Read an explicit key set: one key per line, blank lines and ``#`` allowed."""
    source = Path(path).expanduser()
    try:
        text = source.read_text()
    except OSError as exc:
        raise SystemExit(f"cannot read ticket keys file {source}: {exc}") from exc
    keys: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        key = line.partition("#")[0].strip().upper()
        if not key:
            continue
        if not _KEY_TOKEN.fullmatch(key):
            raise SystemExit(
                f"bad ticket key {key!r} on line {number} of {source}: expected PROJECT-123"
            )
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def merge_ticket_keys(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Union of key sets, first occurrence wins (env + file, prd/18)."""
    return tuple(dict.fromkeys(key for group in groups for key in group))


def filter_scope(ticket_projects: tuple[str, ...], ticket_keys: tuple[str, ...]) -> str:
    """One-line description of what the ticket filter accepts (prd/18)."""
    if ticket_keys:
        return f"keys {','.join(ticket_keys)}"
    return f"projects {','.join(ticket_projects)}"


def _parse_slugs(value: str | list | tuple) -> tuple[str, ...]:
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return tuple(dict.fromkeys(str(i).strip().lower() for i in items if str(i).strip()))


_FALSE = frozenset({"0", "false", "no", "off"})


def _env_flag(value, default: bool = True) -> bool:
    """TOML bool / env string -> bool (``MEGASPEED_PREFILTER=0`` turns it off)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in _FALSE


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
    jira = raw.get("jira", {})
    fixture = raw.get("fixture", {})
    review = raw.get("review", {})
    actors = raw.get("actors", {})
    classify_cfg = raw.get("classify", {})

    repos = [(str(r["project"]), str(r["repo"])) for r in source.get("repos", [])]

    # The self-contained scraper artifact ships no config file: the repo list can
    # come from the environment too.
    #   MEGASPEED_REPOS=DEL/delivery-api,DEL/delivery-web
    repos_env = os.environ.get("MEGASPEED_REPOS", "").strip()
    if repos_env:
        repos = []
        for item in repos_env.split(","):
            project, _, repo = item.strip().partition("/")
            if not project or not repo:
                raise SystemExit(
                    f"bad MEGASPEED_REPOS entry {item!r}: expected PROJECT/REPO,"
                    " e.g. MEGASPEED_REPOS=DEL/delivery-api,DEL/delivery-web"
                )
            repos.append((project, repo))

    fixture_path = os.environ.get("MEGASPEED_FIXTURE_PATH", fixture.get("path") or "")
    # prd/18: an explicit ticket-key set replaces the prefix allowlist. Env wins
    # over the config file per source, and env + file are unioned.
    keys_file = os.environ.get(
        "MEGASPEED_TICKET_KEYS_FILE", filt.get("ticket_keys_file", "") or ""
    ).strip()
    ticket_keys = merge_ticket_keys(
        parse_ticket_keys(os.environ.get("MEGASPEED_TICKET_KEYS", filt.get("ticket_keys", []))),
        load_ticket_keys_file(_resolve(keys_file, base)) if keys_file else (),
    )
    config = Config(
        db_path=_resolve(os.environ.get("MEGASPEED_DB", store.get("db", "data/megaspeed.duckdb")), base),
        identity_path=_resolve(
            os.environ.get("MEGASPEED_IDENTITY", store.get("identity", "var/identity_map.json")), base
        ),
        ticket_regex=os.environ.get("MEGASPEED_TICKET_REGEX", filt.get("ticket_regex", DEFAULT_TICKET_REGEX)),
        key_shape=os.environ.get("MEGASPEED_KEY_SHAPE", filt.get("key_shape", DEFAULT_KEY_SHAPE)),
        ticket_projects=parse_projects(
            os.environ.get("MEGASPEED_TICKET_PROJECTS", filt.get("ticket_projects", list(DEFAULT_TICKET_PROJECTS))),
            required=not ticket_keys,
        ),
        ticket_keys=ticket_keys,
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
        prefilter=_env_flag(os.environ.get("MEGASPEED_PREFILTER"), filt.get("prefilter", True)),
        progress_interval=float(
            os.environ.get("MEGASPEED_PROGRESS_INTERVAL", DEFAULT_PROGRESS_INTERVAL)
        ),
        push_class=_env_flag(os.environ.get("MEGASPEED_PUSH_CLASS"), True),
        git_depth=int(os.environ.get("MEGASPEED_GIT_DEPTH", 128)),
        git_max_depth=int(os.environ.get("MEGASPEED_GIT_MAX_DEPTH", 1024)),
        push_conflict_lines=int(os.environ.get("MEGASPEED_PUSH_CONFLICT_LINES", 20)),
        text_classify=_env_flag(
            os.environ.get("MEGASPEED_TEXT_CLASSIFY"), classify_cfg.get("enabled", True)
        ),
        text_backend=str(os.environ.get("MEGASPEED_TEXT_BACKEND", classify_cfg.get("backend", "")) or ""),
        llm_url=str(os.environ.get("MEGASPEED_LLM_URL", classify_cfg.get("llm_url", classify.DEFAULT_URL))),
        llm_model=str(os.environ.get("MEGASPEED_LLM_MODEL", classify_cfg.get("llm_model", classify.DEFAULT_MODEL))),
        llm_timeout=float(os.environ.get("MEGASPEED_LLM_TIMEOUT", classify_cfg.get("llm_timeout", classify.DEFAULT_TIMEOUT))),
        jira_base_url=os.environ.get("MEGASPEED_JIRA_BASE_URL", jira.get("base_url", "")).rstrip("/"),
        jira_projects=parse_projects(
            os.environ.get("MEGASPEED_JIRA_PROJECTS", jira.get("projects", [])),
            required=False,
        ),
        jira_auth_user=os.environ.get("MEGASPEED_JIRA_USER", ""),
        jira_auth_password=os.environ.get("MEGASPEED_JIRA_PASSWORD", ""),
        jira_auth_token=os.environ.get("MEGASPEED_JIRA_TOKEN", ""),
        jira_page_limit=int(os.environ.get("MEGASPEED_JIRA_PAGE_LIMIT", jira.get("page_limit", 50))),
        jira_min_interval=float(
            os.environ.get("MEGASPEED_JIRA_MIN_INTERVAL", jira.get("min_interval", 0.2))
        ),
        jira_board=int(os.environ.get("MEGASPEED_JIRA_BOARD", jira.get("board", 0))),
        jira_epic_link_field=str(
            os.environ.get("MEGASPEED_JIRA_EPIC_LINK_FIELD", jira.get("epic_link_field", "")) or ""
        ),
        jira_sprint_field=str(
            os.environ.get("MEGASPEED_JIRA_SPRINT_FIELD", jira.get("sprint_field", "")) or ""
        ),
        jira_story_points_field=str(
            os.environ.get("MEGASPEED_JIRA_STORY_POINTS_FIELD", jira.get("story_points_field", "")) or ""
        ),
    )
    if config.base_url and not config.has_credentials:
        raise SystemExit(
            "MEGASPEED_BB_BASE_URL is set but no credentials: set MEGASPEED_BB_TOKEN"
            " or MEGASPEED_BB_USER + MEGASPEED_BB_PASSWORD"
        )
    if config.jira_base_url and not config.has_jira_credentials:
        raise SystemExit(
            "MEGASPEED_JIRA_BASE_URL is set but no credentials: set MEGASPEED_JIRA_TOKEN"
            " or MEGASPEED_JIRA_USER + MEGASPEED_JIRA_PASSWORD"
        )
    try:
        config.llm_url = classify.validate_llm_url(config.llm_url)
    except classify.ClassifyError as exc:
        raise SystemExit(f"bad MEGASPEED_LLM_URL: {exc}") from exc
    return config
