#!/usr/bin/env python3
"""Run the unit-test suite in parallel, stdlib only.

The suite is single-threaded today (``python -m unittest discover -s tests``),
which leaves most of a modern box idle: 1,560 tests, all CPU-bound (DuckDB,
JSON, small simulations). This runner keeps ``unittest``'s semantics and output
but spreads *individual tests* over worker processes.

    .venv/bin/python tests/run_parallel.py              # min(8, cpu) workers
    .venv/bin/python tests/run_parallel.py -j 12
    .venv/bin/python tests/run_parallel.py --times /tmp/test-times.json

``--times`` is the JSON written by a timing run (list of ``[seconds, test_id]``);
it lets the scheduler pack the slow tests first (longest-processing-time), so a
single 20 s test does not decide the wall clock. Without it the ids are spread
round-robin, which is already close.

Exit code is 0 only when every worker reports no failure and no error.

Windows note: ``spawn`` re-imports the test modules in each worker; that is a
second or two per worker, still far cheaper than running the suite serially.
"""

from __future__ import annotations

import argparse
import io
import json
import multiprocessing as mp
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _test_ids(pattern: str = "test*.py") -> list[str]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    suite = unittest.TestLoader().discover(str(ROOT / "tests"), pattern=pattern)
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, unittest.TestSuite):
            for child in node:
                walk(child)
        else:
            out.append(node.id())

    walk(suite)
    return out


def _run_chunk(ids: list[str]) -> dict:
    """One worker: load the ids by name and run them; return a plain dict."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromNames(ids)
    stream = io.StringIO()
    started = time.monotonic()
    loaded = suite.countTestCases()
    result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    elapsed = time.monotonic() - started
    return {
        "tests": result.testsRun,
        "loaded": loaded,
        "elapsed": elapsed,
        "failures": [(str(test), trace) for test, trace in result.failures],
        "errors": [(str(test), trace) for test, trace in result.errors],
        "skipped": [(str(test), reason) for test, reason in result.skipped],
        "unexpected": len(result.unexpectedSuccesses),
        "log": stream.getvalue(),
    }


def _test_id(key: str) -> str:
    """``"m.C.t (pkg.m.C.t)"`` -> ``"m.C.t"``; ids pass through."""
    if "(" in key and key.endswith(")"):
        return key.rsplit("(", 1)[1][:-1]
    return key


def _module_of(test_id: str) -> str:
    return test_id.split(".", 1)[0]


def _weights(times_path: str | None) -> dict[str, float]:
    if not times_path:
        return {}
    try:
        rows = json.loads(Path(times_path).read_text(encoding="utf-8"))
        return {_test_id(name): float(seconds) for seconds, name in rows}
    except (OSError, ValueError, TypeError):
        return {}


def _plan(ids: list[str], jobs: int, times_path: str | None,
          group: str = "module") -> list[list[str]]:
    """Pack the work: whole modules by default, longest first when timed.

    A module is the load unit because its fixtures are shared per process (the
    relabel scrape is ~2.8 s once per worker); splitting a module pays that cost
    in every worker that gets a slice of it.
    """
    weights = _weights(times_path)
    if group == "module":
        units: dict[str, list[str]] = {}
        for name in ids:
            units.setdefault(_module_of(name), []).append(name)
        ordered = sorted(
            units.items(),
            key=lambda item: -sum(weights.get(name, 0.02) for name in item[1]),
        )
        buckets: list[list[str]] = [[] for _ in range(jobs)]
        load = [0.0] * jobs
        for _module, names in ordered:
            target = load.index(min(load))
            buckets[target].extend(names)
            load[target] += sum(weights.get(name, 0.02) for name in names)
        return [bucket for bucket in buckets if bucket]
    if weights:
        ordered = sorted(ids, key=lambda name: -weights.get(name, 0.0))
        buckets = [[] for _ in range(jobs)]
        load = [0.0] * jobs
        for name in ordered:
            target = load.index(min(load))
            buckets[target].append(name)
            load[target] += weights.get(name, 0.05)
        return [bucket for bucket in buckets if bucket]
    return [ids[index::jobs] for index in range(jobs) if ids[index::jobs]]


def run(jobs: int, times_path: str | None, pattern: str = "test*.py",
        group: str = "module") -> int:
    ids = _test_ids(pattern)
    if not ids:
        print("no tests found", file=sys.stderr)
        return 2
    jobs = max(1, min(jobs, len(ids)))
    chunks = _plan(ids, jobs, times_path, group)
    print(f"running {len(ids)} test(s) in {len(chunks)} worker(s) …", flush=True)
    started = time.monotonic()
    context = mp.get_context("fork" if os.name != "nt" else "spawn")
    with context.Pool(len(chunks)) as pool:
        results = pool.map(_run_chunk, chunks)
    total = time.monotonic() - started

    tests = sum(r["tests"] for r in results)
    loaded = sum(r["loaded"] for r in results)
    skipped = [row for r in results for row in r["skipped"]]
    failures = [row for r in results for row in r["failures"]]
    errors = [row for r in results for row in r["errors"]]
    unexpected = sum(r["unexpected"] for r in results)
    for index, result in enumerate(results, start=1):
        print(
            f"  worker {index:2}: {result['tests']:4} test(s) · "
            f"{result['elapsed']:6.1f}s · {len(result['failures'])}F "
            f"{len(result['errors'])}E"
        )
    print(f"\nran {tests} test(s) in {total:.1f}s (parallel, {len(chunks)} workers)"
          f" · loaded {loaded} · {len(skipped)} skip marker(s)")
    for name, reason in skipped:
        print(f"  skipped: {name} — {reason}")
    if loaded != len(ids):
        print(f"  note: {len(ids) - loaded} test(s) were not loaded", file=sys.stderr)
    if failures or errors:
        for label, rows in (("FAIL", failures), ("ERROR", errors)):
            for name, trace in rows:
                print(f"\n{label}: {name}\n{trace}")
        print(f"\nFAILED ({len(failures)} failure(s), {len(errors)} error(s))")
        return 1
    if unexpected:
        print(f"\nunexpected success(es): {unexpected}")
        return 1
    print("OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tests/run_parallel.py")
    ap.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))
    ap.add_argument("--times", default=None, metavar="JSON",
                    help="timings from a serial run: [[seconds, test_id], ...]")
    ap.add_argument("-p", "--pattern", default="test*.py",
                    help="unittest discovery pattern (default test*.py)")
    ap.add_argument("--group", choices=("module", "test"), default="module",
                    help="pack whole modules (default) or individual tests")
    ap.add_argument("--list", action="store_true", help="print the test ids and exit")
    args = ap.parse_args(argv)
    if args.list:
        for name in _test_ids(args.pattern):
            print(name)
        return 0
    return run(args.jobs, args.times, args.pattern, args.group)


if __name__ == "__main__":
    raise SystemExit(main())
