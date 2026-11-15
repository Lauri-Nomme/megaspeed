"""Bot vs human classification (BUG-06).

DC reports the built-in ``bitbucket.system-user`` with ``"type": "SERVICE"``, but
the pipeline pseudonymised it like anyone else ("BiBi") and counted its staleness
comments and auto-declines as human activity -- 4 of 5 declines in the lab corpus
were bot activity. prd/02 has a closed ``bot | human`` dimension precisely so
latency metrics can exclude non-humans.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ingest import fixtures
from ingest.config import Config
from ingest.pipeline import KeyFilter, Reducer, classify_actor, run
from ingest.pseudonym import IdentityTable
from ingest.store import connect, list_objects, verify
from ingest.transport import FixtureTransport

AS_OF = datetime(2026, 11, 15, 12, 0, 0)
SERVICE = {"name": "bitbucket.system-user", "slug": "bitbucket.system-user",
           "displayName": "Bitbucket", "type": "SERVICE"}
HUMAN = {"name": "grace", "displayName": "Grace Hopper", "type": "NORMAL"}


class ActorKindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = Config(
            db_path=root / "store.duckdb",
            identity_path=root / "identity.json",
            ticket_regex=r"\b[A-Z]+-\d+\b",
            key_shape=r"^[A-Z][A-Z0-9_]{0,9}-\d{1,6}$",
            repos=[("ENG", "platform")],
            bot_slugs=("bitbucket.system-user", "jenkins"),
        )
        self.reducer = Reducer(
            self.cfg,
            IdentityTable(self.cfg.identity_path),
            KeyFilter.build(self.cfg.ticket_regex, self.cfg.key_shape, self.cfg.ticket_projects),
            AS_OF,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_service_user_is_a_bot(self):
        self.assertEqual(classify_actor(SERVICE, self.cfg.bot_slugs), "bot")
        # the slug is known even when DC reports type NORMAL (older imports)
        self.assertEqual(classify_actor(dict(SERVICE, type="NORMAL"), self.cfg.bot_slugs), "bot")

    def test_ci_shaped_and_configured_accounts_are_bots(self):
        for slug in ("jenkins", "build-bot", "ci-runner", "svc-deploy", "renovate[bot]"):
            self.assertEqual(
                classify_actor({"name": slug, "displayName": slug, "type": "NORMAL"}, self.cfg.bot_slugs),
                "bot",
                slug,
            )

    def test_people_are_human(self):
        self.assertEqual(classify_actor(HUMAN, self.cfg.bot_slugs), "human")
        self.assertEqual(
            classify_actor({"name": "lauri.nomme", "type": "NORMAL"}, self.cfg.bot_slugs), "human"
        )

    def test_every_event_carries_a_kind_and_persons_are_marked(self):
        bundle = fixtures.generate(n=24, seed=20261115)
        report_run = run(self.cfg, FixtureTransport(bundle), source="fixture")
        self.assertTrue(report_run["verified"], report_run["problems"])
        con = connect(self.cfg.db_path, read_only=True)
        try:
            missing = con.execute(
                "SELECT count(*) FROM events WHERE attributes->>'actor_kind' IS NULL"
            ).fetchone()[0]
            self.assertEqual(missing, 0)
            kinds = {
                row[0]: row[1]
                for row in con.execute(
                    "SELECT actor, attributes->>'actor_kind' FROM events GROUP BY 1, 2"
                ).fetchall()
            }
            self.assertIn("bot", set(kinds.values()))
            self.assertIn("human", set(kinds.values()))
            person_kinds = {p["attributes"]["actor_kind"] for p in list_objects(con, "PERSON")}
            self.assertEqual(person_kinds, {"bot", "human"})
            self.assertTrue(verify(con)["ok"])
        finally:
            con.close()

    def test_fixture_has_service_account_activity(self):
        bundle = fixtures.generate(n=24, seed=20261115)
        bot_actions = {
            activity["action"]
            for project in bundle["projects"]
            for repo in project["repos"]
            for record in repo["pull_requests"]
            for activity in record["activities"]
            if (activity.get("user") or {}).get("type") == "SERVICE"
        }
        self.assertTrue(bot_actions)
        self.assertTrue({"COMMENTED", "DECLINED"} & bot_actions)


if __name__ == "__main__":
    unittest.main()
