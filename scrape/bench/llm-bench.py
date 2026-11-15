#!/usr/bin/env python3
"""Re-runnable benchmark of the local LLM text classifier (prd/16).

This is the harness that produced ``scrape/bench/llm-bench.md``: it walks the
DEL lab corpus, sends a deterministic sample of comment bodies and PR
descriptions through the *real* :class:`ingest.classify.OllamaClassifier`, and
reports latency, fallbacks, schema violations and the label distribution.

It is deliberately **not** a test (the suite uses the deterministic mock) and
it is **not** part of the scrape: it only answers "what does the model cost and
does the closed vocabulary hold on live text?".

Raw-text contract (prd/02)
--------------------------
The corpus text exists in this process only: it is fetched, classified, and
dropped. It is printed to stdout for spot-checking (``--show-text``), but it is
**never** written to a file. ``--append-md`` writes aggregate counts and
timings only -- no comment body, no description, no author.

Usage::

    python3 scrape/bench/llm-bench.py --n 5
    python3 scrape/bench/llm-bench.py --n 20 --desc-n 6 --append-md
    MEGASPEED_LLM_URL=http://10.43.36.10:11434 \
    MEGASPEED_LLM_MODEL=qwen3-30b-a3b:2507 \
        python3 scrape/bench/llm-bench.py --n 20

Configuration (environment wins over the built-in lab defaults)::

    MEGASPEED_LLM_URL        default http://10.43.36.10:11434
    MEGASPEED_LLM_MODEL      default qwen3-30b-a3b:2507
    MEGASPEED_LLM_TIMEOUT    default 180 s
    MEGASPEED_BB_BASE_URL    default http://127.0.0.1:7991 (bb-lab-b)
    MEGASPEED_BB_USER        default admin
    MEGASPEED_BB_PASSWORD    default admin-lab-pw
    MEGASPEED_REPOS          default DEL/delivery-api,DEL/delivery-web

Stdlib ``urllib`` only, same as the classifier and the scrape transport.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# The bench lives two directories below the repo root, outside the ingest
# package; the repo root is what makes ``import ingest`` work.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ingest import classify  # noqa: E402
from ingest.bitbucket import BitbucketClient  # noqa: E402
from ingest.transport import HttpTransport  # noqa: E402

DEFAULT_BB_URL = "http://127.0.0.1:7991"
DEFAULT_BB_USER = "admin"
DEFAULT_BB_PASSWORD = "admin-lab-pw"
DEFAULT_REPOS = "DEL/delivery-api,DEL/delivery-web"
DEFAULT_MD = Path(__file__).resolve().parent / "llm-bench.md"

#: Warm-up text for the first (cold) call. Fixed, not corpus text: the model
#: load is the thing being paid for, and it must not skew a real sample.
WARMUP_TEXT = "warmup: is this a question?"


# --------------------------------------------------------------------------- #
# fetch -- the existing client, no reinvented REST
# --------------------------------------------------------------------------- #
def _parse_repos(value: str) -> list[tuple[str, str]]:
    repos: list[tuple[str, str]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        project, sep, repo = item.partition("/")
        if not sep or not project or not repo:
            raise SystemExit(f"bad repo {item!r}: expected PROJECT/REPO")
        repos.append((project, repo))
    if not repos:
        raise SystemExit("no repos configured (MEGASPEED_REPOS)")
    return repos


def fetch_corpus(args) -> tuple[list[str], list[str], dict[str, int]]:
    """Return ``(comment_texts, description_texts, counts)`` from the live DC.

    Walks the normal scrape path -- ``BitbucketClient.iter_pull_requests`` and
    ``iter_activities`` -- so paging and URL building are exercised exactly as
    in a real run. Text stays in memory; nothing is cached or written.
    """
    transport = HttpTransport(
        args.bb_url,
        user=args.bb_user,
        password=args.bb_password,
        timeout=args.bb_timeout,
    )
    client = BitbucketClient(transport, page_limit=args.page_limit)
    comments: list[str] = []
    descriptions: list[str] = []
    prs_seen = 0
    for project, repo in _parse_repos(args.repos):
        for pr in client.iter_pull_requests(project, repo):
            if args.pr_limit and prs_seen >= args.pr_limit:
                break
            prs_seen += 1
            body = str(pr.get("description") or "").strip()
            if body:
                descriptions.append(body)
            for activity in client.iter_activities(project, repo, int(pr["id"])):
                action = activity.get("commentAction")
                if action not in (None, "ADDED", "UPDATED"):
                    continue  # deleted comments are not dialogue
                text = str((activity.get("comment") or {}).get("text") or "").strip()
                if text:
                    comments.append(text)
    counts = {
        "prs": prs_seen,
        "comments": len(comments),
        "descriptions": len(descriptions),
    }
    return comments, descriptions, counts


def stride_sample(items: list[str], n: int) -> list[str]:
    """Every *k*-th item: a deterministic spread, not the newest n.

    Sampling the head would over-represent the most recently updated PRs; the
    stride covers the whole corpus walk and is reproducible for a given corpus.
    """
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return list(items)
    return [items[(i * len(items)) // n] for i in range(n)]


# --------------------------------------------------------------------------- #
# classify -- the real backend, instrumented without changing it
# --------------------------------------------------------------------------- #
class ProbeClassifier(classify.OllamaClassifier):
    """``OllamaClassifier`` that exposes what one call did.

    ``classify`` swallows backend failures on purpose (a scrape must not die on
    one comment), so the bench cannot tell a network fallback from a schema
    violation after the fact. This subclass records the raw answer and whether
    the fallback counter moved; the loop re-parses the answer with
    :func:`ingest.classify.parse_labels` to attribute the failure. Behaviour is
    otherwise identical: same request body, same fallback, same counters.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_content: str | None = None
        self.last_fell_back = False

    def classify(self, text: str, kind: str) -> dict:
        self.last_content = None
        before = self.fallbacks
        labels = super().classify(text, kind)
        self.last_fell_back = self.fallbacks > before
        return labels

    def _chat(self, body: dict):
        content = super()._chat(body)
        self.last_content = content
        return content


def run_sample(probe: ProbeClassifier, texts: list[str], kind: str) -> dict:
    """Time one ``classify`` per text and collect aggregate stats."""
    latencies: list[float] = []
    lengths: list[int] = []
    fallbacks = 0
    violations = 0
    for text in texts:
        start = time.perf_counter()
        probe.classify(text, kind)
        latencies.append(time.perf_counter() - start)
        lengths.append(len(text))
        if probe.last_fell_back:
            fallbacks += 1
            # A fallback with an answer in hand is a schema violation; no
            # answer at all means the box was unreachable / timed out.
            if probe.last_content is not None:
                try:
                    classify.parse_labels(kind, probe.last_content)
                except classify.ClassifyError:
                    violations += 1
    return {
        "kind": kind,
        "n": len(texts),
        "latencies": latencies,
        "min_chars": min(lengths) if lengths else 0,
        "max_chars": max(lengths) if lengths else 0,
        "fallbacks": fallbacks,
        "violations": violations,
    }


# --------------------------------------------------------------------------- #
# report -- aggregates only, never text
# --------------------------------------------------------------------------- #
def _secs(values: list[float]) -> tuple[str, str, str]:
    if not values:
        return "—", "—", "—"
    return (
        f"{statistics.median(values):.2f} s",
        f"{statistics.fmean(values):.2f} s",
        f"{max(values):.2f} s",
    )


def print_summary(args, probe, counts, samples, warmup_ok: bool) -> None:
    print("megaspeed LLM bench — DEL comments (prd/16)")
    print()
    print(f"endpoint   {probe.url}  model {probe.model}")
    print(
        "corpus     "
        f"{args.repos}: {counts['prs']} PRs, {counts['comments']} comments, "
        f"{counts['descriptions']} descriptions"
    )
    print(
        "sample     "
        + ", ".join(
            f"{s['n']} {s['kind']}s (of {counts['comments' if s['kind'] == 'comment' else 'descriptions']}, "
            f"every k-th)"
            for s in samples
        )
    )
    print(f"warmup     {'1 untimed call' if args.warmup else 'off'}"
          + ("" if warmup_ok else "  (FAILED — model may be cold or down)"))
    print()
    print(f"{'call':<12}{'n':>4}{'median':>10}{'mean':>10}{'max':>10}   chars")
    for s in samples:
        median, mean, maximum = _secs(s["latencies"])
        print(
            f"{s['kind']:<12}{s['n']:>4}{median:>10}{mean:>10}{maximum:>10}"
            f"   {s['min_chars']}–{s['max_chars']}"
        )
    print()
    total = sum(s["n"] for s in samples)
    fallbacks = sum(s["fallbacks"] for s in samples)
    violations = sum(s["violations"] for s in samples)
    print(
        f"fallbacks {fallbacks}/{total}   schema violations {violations}/{total}   "
        f"parsed {total - fallbacks}/{total}"
    )
    print()
    print("labels")
    if probe.labels:
        for key, value in sorted(probe.labels.items()):
            print(f"  {key:<28} {value}")
    else:
        print("  (none)")
    if total and fallbacks == total:
        print("\nwarning: every call fell back — the model was not exercised.")
    elif probe.down:
        print(
            f"\nnote: the circuit opened after {probe.max_failures} consecutive failures; "
            "the rest of the sample used deterministic fallback labels."
        )


def append_markdown(path: Path, probe, counts, samples) -> None:
    """Append an aggregate-only section; safe to commit (no raw text)."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n_comments = samples[0]["n"] if samples else 0
    n_descriptions = samples[1]["n"] if len(samples) > 1 else 0
    lines = [
        "",
        f"## Re-run — {stamp} (`scrape/bench/llm-bench.py`)",
        "",
        f"`{probe.url}`, `{probe.model}`. Corpus: {counts['prs']} PRs, "
        f"{counts['comments']} comments, {counts['descriptions']} descriptions; "
        f"sample: {n_comments} comment{'s' if n_comments != 1 else ''} + "
        f"{n_descriptions} description{'s' if n_descriptions != 1 else ''} "
        "(deterministic every-*k*-th spread). "
        f"Fallbacks: {sum(s['fallbacks'] for s in samples)}. "
        f"Schema violations: {sum(s['violations'] for s in samples)}.",
        "",
        "| Call | n | median | mean | max | chars |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for s in samples:
        median, mean, maximum = _secs(s["latencies"])
        lines.append(
            f"| `{s['kind']}` | {s['n']} | {median} | {mean} | {maximum} "
            f"| {s['min_chars']}–{s['max_chars']} |"
        )
    lines += [
        "",
        "Labels: " + (", ".join(
            f"`{key}` ×{value}" for key, value in sorted(probe.labels.items())
        ) or "(none)"),
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    path.write_text(existing + "\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark the local Ollama classifier on live DEL text "
        "(aggregates only; raw text is never written).",
    )
    parser.add_argument("--n", type=int, default=20, help="comments to sample (default 20)")
    parser.add_argument(
        "--desc-n", type=int, default=6, help="descriptions to sample (default 6)"
    )
    parser.add_argument(
        "--pr-limit", type=int, default=0, help="walk at most N PRs (default all)"
    )
    parser.add_argument(
        "--no-warmup", dest="warmup", action="store_false", help="skip the untimed cold call"
    )
    parser.add_argument(
        "--show-text", action="store_true", help="print sampled text to stdout (never to a file)"
    )
    parser.add_argument(
        "--append-md",
        nargs="?",
        const=str(DEFAULT_MD),
        default=None,
        metavar="PATH",
        help=f"append an aggregate-only section to a markdown file (default {DEFAULT_MD.name})",
    )
    parser.add_argument(
        "--llm-url", default=os.environ.get("MEGASPEED_LLM_URL", classify.DEFAULT_URL)
    )
    parser.add_argument(
        "--llm-model", default=os.environ.get("MEGASPEED_LLM_MODEL", classify.DEFAULT_MODEL)
    )
    parser.add_argument(
        "--llm-timeout",
        type=float,
        default=float(os.environ.get("MEGASPEED_LLM_TIMEOUT", classify.DEFAULT_TIMEOUT)),
    )
    parser.add_argument(
        "--bb-url", default=os.environ.get("MEGASPEED_BB_BASE_URL", DEFAULT_BB_URL)
    )
    parser.add_argument(
        "--bb-user", default=os.environ.get("MEGASPEED_BB_USER", DEFAULT_BB_USER)
    )
    parser.add_argument(
        "--bb-password",
        default=os.environ.get("MEGASPEED_BB_PASSWORD", DEFAULT_BB_PASSWORD),
    )
    parser.add_argument(
        "--bb-timeout", type=float, default=float(os.environ.get("MEGASPEED_BB_TIMEOUT", 120))
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=int(os.environ.get("MEGASPEED_PAGE_LIMIT", 100)),
        help="activities page size (default 100, fewer requests)",
    )
    parser.add_argument(
        "--repos", default=os.environ.get("MEGASPEED_REPOS", DEFAULT_REPOS),
        help="comma-separated PROJECT/REPO list",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.n < 0 or args.desc_n < 0:
        raise SystemExit("--n / --desc-n must be >= 0")

    try:
        comments, descriptions, counts = fetch_corpus(args)
    except Exception as exc:  # noqa: BLE001 - a bench must report, not traceback
        print(f"fetch failed: {exc}", file=sys.stderr)
        return 2

    comment_sample = stride_sample(comments, args.n)
    description_sample = stride_sample(descriptions, args.desc_n)
    if not comment_sample and not description_sample:
        print("no text sampled: is the corpus reachable and non-empty?", file=sys.stderr)
        return 2

    if args.show_text:
        # stdout only: the raw text dies with the process (prd/02).
        for kind, texts in (("comment", comment_sample), ("description", description_sample)):
            for index, text in enumerate(texts, 1):
                print(f"[{kind} {index}/{len(texts)}] {text}")

    probe = ProbeClassifier(args.llm_url, args.llm_model, timeout=args.llm_timeout)
    warmup_ok = True
    if args.warmup:
        probe.classify(WARMUP_TEXT, classify.KIND_COMMENT)
        warmup_ok = not probe.last_fell_back
        # The warm-up text is not corpus text: keep it out of the distribution.
        probe.labels.clear()
        probe.calls = 0

    samples = []
    if comment_sample:
        samples.append(run_sample(probe, comment_sample, classify.KIND_COMMENT))
    if description_sample:
        samples.append(run_sample(probe, description_sample, classify.KIND_DESCRIPTION))

    print_summary(args, probe, counts, samples, warmup_ok)
    if args.append_md:
        append_markdown(Path(args.append_md), probe, counts, samples)
        print(f"\nappended aggregate summary to {args.append_md}")

    if all(s["fallbacks"] == s["n"] for s in samples):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
