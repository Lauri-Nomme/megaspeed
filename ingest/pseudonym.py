"""Actor pseudonyms.

Contract: downstream sees ``LaNo`` and nothing else. The mapping
(slug/email/display name -> pseudonym) lives *only* in the raw-zone identity
table, which the server never opens and git never carries.

Scheme (prd/02): first two letters of the given name + first two of the
surname, capitalised. Collisions get a deterministic numeric suffix, allocated
in first-seen order and persisted, so a person's pseudonym is stable forever.

Two sources, one table (prd/03 "Identity resolution"): a Bitbucket actor is keyed
by ``bb:<slug>`` and a Jira actor by ``jira:<username>``, and the first time one
source sees an actor the other source already knows, the two records are
*linked* when they obviously describe the same human (same e-mail address, same
e-mail local part / username as the Bitbucket slug, or the same display name).
That is what makes "the same person" consistent across a Jira assignee and a git
author. Linking only ever crosses sources, and an ambiguous match allocates a
fresh pseudonym instead of guessing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_SPLIT = re.compile(r"[\s._\-/@+]+")
_NON_ALPHA = re.compile(r"[^A-Za-z]")

GIVEN_NAME, SURNAME, DISPLAY, EMAIL = "given", "surname", "display", "email"

BITBUCKET = "bb"
JIRA = "jira"


def base_pseudonym(given: str, surname: str) -> str:
    a = _NON_ALPHA.sub("", given)[:2].capitalize() or "Xx"
    b = _NON_ALPHA.sub("", surname)[:2].capitalize() or "Xx"
    return a + b


def split_name(user: dict) -> tuple[str, str]:
    """Best-effort name split from a user record (Bitbucket or Jira).

    ``displayName`` first, then the slug/username, then the email local part.
    Single token names are duplicated (``lauri`` -> ``LaLa``) rather than
    guessed.
    """
    for candidate in (user.get("displayName"), user.get("name"), str(user.get("emailAddress") or "").split("@")[0]):
        tokens = [t for t in _SPLIT.split(str(candidate or "")) if t]
        if len(tokens) >= 2:
            return tokens[0], tokens[-1]
        if len(tokens) == 1:
            return tokens[0], tokens[0]
    return "unknown", "actor"


def actor_key(user: dict) -> str:
    """Stable raw-zone identity key for a Bitbucket actor.

    Slug first, then the email local part (DC slugs are usually the local part),
    then the display name. This keeps "lauri.nomme", "lauri.nomme@corp" and a
    slug-less commit author pointing at one identity.
    """
    slug = str(user.get("name") or "").strip()
    email = str(user.get("emailAddress") or "").strip().lower()
    if slug:
        return f"{BITBUCKET}:" + slug
    if email:
        return f"{BITBUCKET}:" + email.split("@", 1)[0]
    return f"{BITBUCKET}:" + (str(user.get("displayName") or "").strip() or "unknown")


def jira_actor_key(user: dict) -> str:
    """Stable raw-zone identity key for a Jira actor.

    The username (``name`` in DC, ``accountId`` in Cloud) first, then the e-mail
    local part, then the display name. The internal ``key`` (``JIRAUSER10102``)
    is *not* used: it is an installation-local number and would break pseudonym
    continuity when a lab is re-seeded.
    """
    username = str(user.get("name") or user.get("accountId") or "").strip()
    email = str(user.get("emailAddress") or "").strip().lower()
    if username:
        return f"{JIRA}:" + username
    if email:
        return f"{JIRA}:" + email.split("@", 1)[0]
    return f"{JIRA}:" + (str(user.get("displayName") or "").strip() or "unknown")


def _normalise(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _identity_tokens(key: str, entry: dict) -> set[str]:
    """Everything that can identify an existing entry, for cross-source linking.

    The key's own token (``bb:lauri.nomme`` -> ``lauri.nomme``), the e-mail
    address and its local part, and the normalised display name.
    """
    tokens = {key.partition(":")[2].strip().lower()}
    email = str(entry.get("email") or "").strip().lower()
    if email:
        tokens.add(email)
        tokens.add(email.split("@", 1)[0])
    display = _normalise(entry.get("display"))
    if display:
        tokens.add(display)
    return {token for token in tokens if token}


def _user_tokens(user: dict) -> set[str]:
    """The same tokens, computed for the actor we are resolving."""
    tokens: set[str] = set()
    username = str(user.get("name") or user.get("accountId") or "").strip().lower()
    if username:
        tokens.add(username)
    email = str(user.get("emailAddress") or "").strip().lower()
    if email:
        tokens.add(email)
        tokens.add(email.split("@", 1)[0])
    display = _normalise(user.get("displayName"))
    if display:
        tokens.add(display)
    return {token for token in tokens if token}


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

    # -- resolution ------------------------------------------------------
    def pseudonym(self, user: dict, source: str = BITBUCKET) -> str:
        """Pseudonym for ``user``; ``source`` is ``bb`` (default) or ``jira``.

        The exact actor key always wins. Otherwise the actor is *linked* to an
        identity from the other source when the two records obviously describe
        the same human (prd/03), and a fresh pseudonym is allocated when there
        is no such match. Linking only ever crosses sources, so a single-source
        run behaves exactly as it did before the Jira source existed -- and the
        order in which the two scrapes happen does not matter.
        """
        key = jira_actor_key(user) if source == JIRA else actor_key(user)
        existing = self.entries.get(key)
        if existing:
            return existing["pseudonym"]
        linked = self._link(user, key)
        if linked is not None:
            return linked
        return self._allocate(user, key)

    def pseudonym_jira(self, user: dict) -> str:
        return self.pseudonym(user, source=JIRA)

    def _link(self, user: dict, key: str) -> str | None:
        """Link ``key`` to exactly one existing identity from the other source."""
        wanted = _user_tokens(user)
        if not wanted:
            return None
        source = key.partition(":")[0]
        matches: dict[str, str] = {}
        for other_key, entry in self.entries.items():
            if other_key.partition(":")[0] == source:
                continue
            if _identity_tokens(other_key, entry) & wanted:
                matches.setdefault(entry["pseudonym"], other_key)
        if len(matches) != 1:
            # No match: a new person. Several matches: ambiguous, so allocate a
            # fresh pseudonym rather than silently merging two humans.
            return None
        pseudonym, other_key = next(iter(matches.items()))
        self.entries[key] = {
            "pseudonym": pseudonym,
            "given": self.entries[other_key].get("given", ""),
            "surname": self.entries[other_key].get("surname", ""),
            "display": user.get("displayName") or "",
            "email": user.get("emailAddress") or "",
            "linked_to": other_key,
        }
        # The canonical key stays the first one seen, so collision suffixes are
        # still allocated against the same person.
        self.used.setdefault(pseudonym, other_key)
        self.dirty = True
        return pseudonym

    def _allocate(self, user: dict, key: str) -> str:
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
                        "_comment": "RAW ZONE ONLY. Maps Bitbucket + Jira identities to pseudonyms. Never served.",
                        "actors": self.entries,
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
            self.dirty = False
