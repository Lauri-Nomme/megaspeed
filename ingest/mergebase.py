"""Batch merge-base via hans's ``bb-merge-base`` accelerator (libgit2).

``git merge-base`` is a process per query: ~0.3 s on a large mirror, and the
hans walk needs one fork point per PR. ``bb-merge-base`` opens the repository
**once** and answers ``<from> <to>`` pairs on stdin. It is a **batch** tool: its
stdout is buffered and flushed at EOF, so the pairs are fed in one run and the
answers read after it exits (an interactive request/response would deadlock).

Binary discovery (first hit wins, host platform only):

1. ``MEGASPEED_BB_MERGE_BASE`` (an explicit path);
2. ``bb-merge-base[.exe]`` next to the capture or one of its parents;
3. the usual hans checkout layouts
   (``tools/bb-merge-base/{dist,target/...}/...``);
4. ``bb-merge-base`` on ``PATH``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def batch_merge_bases(
    binary: str | Path,
    gitdir: str | Path,
    pairs: list[tuple[str, str]] | tuple[tuple[str, str], ...],
    *,
    bare: bool = True,
    timeout: float = 300.0,
) -> list[str] | None:
    """One ``bb-merge-base`` run for every pair; ``None`` when it cannot run.

    Returns one base per pair (an empty string when the history has none).
    """
    if not pairs:
        return []
    payload = "".join(f"{first} {second}\n" for first, second in pairs)
    command = [str(binary), str(gitdir), *(["--bare"] if bare else [])]
    try:
        process = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if process.returncode != 0:
        return None
    lines = process.stdout.splitlines()
    if len(lines) < len(pairs):
        lines.extend([""] * (len(pairs) - len(lines)))
    return [line.strip() for line in lines[: len(pairs)]]


def find_binary(root: str | Path | None = None) -> str | None:
    """Locate a **host-compatible** ``bb-merge-base`` (or ``None``)."""
    explicit = os.environ.get("MEGASPEED_BB_MERGE_BASE", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit
    if sys.platform == "win32":
        names = ("bb-merge-base.exe", "bb-merge-base-windows-x86_64.exe")
        targets = (
            "dist/bb-merge-base-windows-x86_64.exe",
            "target/x86_64-pc-windows-gnu/release/bb-merge-base.exe",
            "target/release/bb-merge-base.exe",
        )
    elif sys.platform == "darwin":
        names = ("bb-merge-base", "bb-merge-base-macos")
        targets = (
            "dist/bb-merge-base-macos",
            "target/release/bb-merge-base",
            "target/aarch64-apple-darwin/release/bb-merge-base",
            "target/x86_64-apple-darwin/release/bb-merge-base",
        )
    else:
        names = ("bb-merge-base", "bb-merge-base-linux-x86_64")
        targets = (
            "dist/bb-merge-base-linux-x86_64",
            "target/release/bb-merge-base",
            "target/x86_64-unknown-linux-gnu/release/bb-merge-base",
        )
    roots: list[Path] = []
    if root is not None:
        base = Path(root).resolve()
        roots = [base, *list(base.parents)[:3]]
    for directory in roots:
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
        tools = directory / "tools" / "bb-merge-base"
        for relative in targets:
            candidate = tools / relative
            if candidate.is_file():
                return str(candidate)
    return shutil.which("bb-merge-base")
