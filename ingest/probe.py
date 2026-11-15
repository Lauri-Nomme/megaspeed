"""Read-only probe of a live Bitbucket instance (no store writes).

    python3 -m ingest --live --repo FIX/golden --probe
    python3 -m ingest --live --repo FIX/golden --probe --probe-bypass-filter

``--probe`` answers "can we reach it, and what does the API actually return?":
auth, paging, PR/activity/commit shapes, and how many PRs the ticket-key filter
would keep. ``--probe-bypass-filter`` additionally runs the reducer *in memory*
over the unfiltered PRs and prints sample events, so the mapping can be checked
against a corpus that has no ticket keys. Nothing is written to the store, and
the identity map is not saved.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from .bitbucket import BitbucketClient
from .config import Config
from .pipeline import KeyFilter, Reducer, Stats
from .pseudonym import IdentityTable

KNOWN_ACTIONS = {"OPENED", "RESCOPED", "COMMENTED", "APPROVED", "UNAPPROVED", "NEEDS_WORK",
                 "REVIEWER_UPDATED", "MERGED", "DECLINED"}


def probe(cfg: Config, client: BitbucketClient, bypass_filter: bool = False) -> dict:
    as_of = datetime.now(timezone.utc).replace(tzinfo=None)
    key_filter = KeyFilter.build(cfg.ticket_regex, cfg.key_shape)
    ident = IdentityTable(cfg.identity_path)
    reducer = Reducer(cfg, ident, key_filter, as_of)
    stats = Stats()
    repos: list[dict] = []

    for project, repo in cfg.repos:
        label = f"{project}/{repo}"
        entry = {
            "repo": label,
            "prs": 0,
            "by_state": Counter(),
            "actions": Counter(),
            "ignored_actions": Counter(),
            "commits": 0,
            "matched_prs": [],
            "unmatched_prs": [],
            "keys": set(),
        }
        for pr in client.pull_requests(project, repo):
            entry["prs"] += 1
            entry["by_state"][pr.pr.get("state", "?")] += 1
            entry["commits"] += len(pr.commits)
            keys = key_filter.keys(
                (pr.pr.get("fromRef") or {}).get("displayId"),
                pr.pr.get("title"),
                pr.pr.get("description"),
                *[c.get("message") for c in pr.commits],
            )
            if keys:
                entry["matched_prs"].append(pr.id)
                entry["keys"].update(keys)
            else:
                entry["unmatched_prs"].append(pr.id)

            pr.activities = list(client.iter_activities(project, repo, pr.id))
            for activity in pr.activities:
                action = str(activity.get("action") or "?")
                entry["actions"][action] += 1
                if action not in KNOWN_ACTIONS:
                    entry["ignored_actions"][action] += 1

            if bypass_filter:
                reducer.reduce(pr, keys, pr.activities, stats)

        entry["keys"] = sorted(entry["keys"])
        repos.append(entry)

    sample = []
    for event in stats.events[:8]:
        sample.append(
            {
                "ts": event["ts"].isoformat(timespec="seconds") + "Z",
                "activity": event["activity"],
                "actor": event["actor"],
                "objects": [oid for (key, _t, oid) in stats.relations if key == event["event_key"]],
                "attributes": event["attributes"],
            }
        )

    return {
        "as_of": as_of.isoformat(timespec="seconds") + "Z",
        "requests": client.requests,
        "repos": repos,
        "bypass_filter": bypass_filter,
        "dry_run_events": len(stats.events),
        "dry_run_event_activities": dict(sorted(Counter(e["activity"] for e in stats.events).items())),
        "dry_run_pseudonyms": sorted(stats.people),
        "dry_run_sample": sample,
        "ticket_regex": cfg.ticket_regex,
        "key_shape": cfg.key_shape,
    }
