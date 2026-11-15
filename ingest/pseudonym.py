"""Actor pseudonyms.

Contract: downstream sees ``LaNo`` and nothing else. The mapping
(slug/email/display name -> pseudonym) lives *only* in the raw-zone identity
table, which the server never opens and git never carries.

Scheme (prd/02): first two letters of the given name + first two of the
surname, capitalised. Collisions get a deterministic numeric suffix, allocated
in first-seen order and persisted, so a person's pseudonym is stable forever.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_SPLIT = re.compile(r"[\s._\-/@+]+")
_NON_ALPHA = re.compile(r"[^A-Za-z]")

GIVEN_NAME, SURNAME, DISPLAY, EMAIL = "given", "surname", "display", "email"


def base_pseudonym(given: str, surname: str) -> str:
    a = _NON_ALPHA.sub("", given)[:2].capitalize() or "Xx"
    b = _NON_ALPHA.sub("", surname)[:2].capitalize() or "Xx"
    return a + b


def split_name(user: dict) -> tuple[str, str]:
    """Best-effort name split from a Bitbucket user record.

    ``displayName`` first, then the slug, then the email local part. Single
    token names are duplicated (``lauri`` -> ``LaLa``) rather than guessed.
    """
    for candidate in (user.get("displayName"), user.get("name"), str(user.get("emailAddress") or "").split("@")[0]):
        tokens = [t for t in _SPLIT.split(str(candidate or "")) if t]
        if len(tokens) >= 2:
            return tokens[0], tokens[-1]
        if len(tokens) == 1:
            return tokens[0], tokens[0]
    return "unknown", "actor"


def actor_key(user: dict) -> str:
    """Stable raw-zone identity key for a Bitbucket actor."""
    slug = str(user.get("name") or "").strip()
    email = str(user.get("emailAddress") or "").strip().lower()
    return "bb:" + (slug or email or str(user.get("displayName") or "").strip() or "unknown")


class IdentityTable:
    """Raw-zone slug/email -> pseudonym table (a local JSON file)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.entries: dict[str, dict] = {}
        self.used: dict[str, str] = {}
        self.dirty = False
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
            self.entries = data.get("actors", {})
            for key, entry in self.entries.items():
                self.used[entry["pseudonym"]] = key

    def pseudonym(self, user: dict) -> str:
        key = actor_key(user)
        existing = self.entries.get(key)
        if existing:
            return existing["pseudonym"]
        given, surname = split_name(user)
        base = base_pseudonym(given, surname)
        pseudonym, n = base, 2
        while pseudonym in self.used and self.used[pseudonym] != key:
            pseudonym = f"{base}{n}"
            n += 1
        self.entries[key] = {
            "pseudonym": pseudonym,
            "given": given,
            "surname": surname,
            "display": user.get("displayName") or "",
            "email": user.get("emailAddress") or "",
        }
        self.used[pseudonym] = key
        self.dirty = True
        return pseudonym

    def save(self) -> None:
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "_comment": "RAW ZONE ONLY. Maps Bitbucket identities to pseudonyms. Never served.",
                        "actors": self.entries,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.dirty = False
