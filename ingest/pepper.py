"""The per-tenant **ref-digest pepper**: raw-zone key material for prd/22.

``ref_digest`` (:mod:`ingest.pipeline`) exists to hide a branch name, and a DC
branch is the ticket summary in slug form (``DEL-101-add-login-form-validation``).
A plain hash does not hide it: both inputs (``repo`` and the ticket key) are in
the clear in the same store, so the branch name is a *dictionary* lookup away.
The fix is keying: an HMAC with a secret that never leaves the raw zone.

This module owns that secret:

* it lives next to the other raw-zone files (``<state-dir>/pepper.json``), is
  written ``0600``, and **never** enters a bundle or the store -- the tgz
  manifest records only :func:`key_id`, a non-reversible label that says *which*
  pepper produced the digests;
* it is per tenant (one state dir per tenant), so the same repo/branch in two
  tenants produces unrelated digests and a precomputed table does not transfer;
* it is created on first use with :func:`secrets.token_bytes` (256 bits).

What is left, and is inherent to the feature: **equal digests mean "same
branch"** -- that is exactly what the stack DAG asks. Nothing else is derivable
without the pepper. Losing the pepper loses the ability to match *new* digests
against rows already in the store (the old rows keep their old digests), which
is why the scrape records :func:`key_id` and refuses to silently swap the key
under a state dir that already used a different one.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

PEPPER_FILE = "pepper.json"
SCHEMA_VERSION = 1
#: 256 bits of HMAC key. The digest output is 128 bits (see ``ref_digest``).
PEPPER_BYTES = 32
MIN_PEPPER_BYTES = 16


class PepperError(RuntimeError):
    """The pepper file exists but cannot be trusted (bad shape, unreadable)."""


def load_pepper(path: str | Path) -> bytes:
    """Read the pepper at ``path``, creating it (mode ``0600``) when absent.

    Creation is ``O_EXCL``: two scrapes racing on one state dir cannot end up
    with different keys in memory and on disk -- the loser re-reads the winner's
    file. (Two scrapes sharing a state dir is still unsupported; see
    ``ingest.scrape``'s module docstring.)
    """
    path = Path(path)
    if path.exists():
        return _read(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pepper = secrets.token_bytes(PEPPER_BYTES)
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read(path)
    with os.fdopen(handle, "w") as stream:
        stream.write(
            json.dumps(
                {
                    "_comment": (
                        "RAW ZONE: HMAC pepper for prd/22 ref digests. Keep it with"
                        " the state dir; never transfer it in a bundle. Losing it"
                        " means old digests can no longer be matched."
                    ),
                    "schema_version": SCHEMA_VERSION,
                    "pepper": pepper.hex(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
    return pepper


def _read(path: Path) -> bytes:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise PepperError(f"{path}: not a readable pepper file ({exc})") from exc
    if not isinstance(payload, dict):
        raise PepperError(f"{path}: not a JSON object")
    value = payload.get("pepper")
    if not isinstance(value, str) or not value.strip():
        raise PepperError(f"{path}: no 'pepper' value")
    try:
        pepper = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise PepperError(f"{path}: 'pepper' is not hex ({exc})") from exc
    if len(pepper) < MIN_PEPPER_BYTES:
        raise PepperError(
            f"{path}: pepper is {len(pepper)} byte(s); need at least"
            f" {MIN_PEPPER_BYTES} (128 bits)"
        )
    return pepper


def key_id(pepper: bytes) -> str:
    """A stable, non-reversible label for *which* pepper made a digest.

    Recorded in the scrape state (and the tgz manifest) so an operator can see
    that a re-scrape used the same key, without the key ever crossing the
    boundary. 16 hex chars, one-way: it reveals nothing about the pepper.
    """
    return hashlib.sha256(b"megaspeed-ref-digest-key-id\x00" + pepper).hexdigest()[:16]


__all__ = [
    "MIN_PEPPER_BYTES",
    "PEPPER_BYTES",
    "PEPPER_FILE",
    "PepperError",
    "key_id",
    "load_pepper",
]
