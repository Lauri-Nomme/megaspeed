"""prd/75: the deterministic non-review classifier (no model, closed labels)."""

from __future__ import annotations

import unittest

from ingest import citext

COVERAGE_VARIANTS = (
    "Partial code coverage [report](http://x/C).\nCoverage info for the first build:\n"
    "\ninstruction coverage is 0%\nline coverage is 0%\nbranch coverage is 0%",
    "Partial code coverage [report](http://x/C).\nCoverage info for the first build:\n"
    "\n:100: % coverage :+1:",
    "Partial code coverage [report](http://x/C).\nCoverage info for the first build:\n"
    "\ninstructions covered ratio is 0.7\nbranches covered ratio is 0.4\n"
    "lines covered ratio is 0.5",
    "Partial code coverage [report](http://x/C).\nCoverage info for the first build:\n"
    "\ninstruction coverage is 71%\nline coverage is 83%",
    "Partial code coverage [report](http://x/C).\nCoverage info for the first build:",
    "Partial code coverage [report](http://x/C).\nCoverage info for the first build:\n"
    "\nbranches covered ratio is 0.9",
)


class CoverageTests(unittest.TestCase):
    def test_every_variant_is_the_coverage_template(self):
        for text in COVERAGE_VARIANTS:
            self.assertEqual(citext.ci_kind(text)[0], "coverage", text[:40])

    def test_zero_report_is_the_lowest_metric(self):
        self.assertEqual(citext.ci_kind(COVERAGE_VARIANTS[0])[2], 0.0)
        self.assertTrue(citext.coverage_metrics(COVERAGE_VARIANTS[0]))

    def test_ratios_are_scaled_to_percent(self):
        metrics = citext.coverage_metrics(COVERAGE_VARIANTS[2])
        self.assertEqual(metrics["instruction"], 70.0)
        self.assertEqual(metrics["line"], 50.0)

    def test_emoji_form_is_100_and_carries_no_number(self):
        template, issues, low = citext.ci_kind(COVERAGE_VARIANTS[1])
        self.assertEqual((template, issues, low), ("coverage", None, 100.0))

    def test_header_only_has_no_metric(self):
        self.assertIsNone(citext.ci_kind(COVERAGE_VARIANTS[4])[2])


class QodanaTests(unittest.TestCase):
    def test_clean(self):
        text = ("#### ✅ Qodana: no new issues found.\n\n[Full Qodana report](http://x)")
        self.assertEqual(citext.ci_kind(text), ("qodana_clean", 0, None))

    def test_finding_carries_the_count(self):
        text = ("#### ⚠️ Qodana detected 1021 new issue(s)\n\nPlease do not merge")
        self.assertEqual(citext.ci_kind(text), ("qodana_issues", 1021, None))

    def test_a_human_mentioning_qodana_is_not_a_template(self):
        self.assertEqual(
            citext.ci_kind("the qodana report is wrong about this one")[0], "unknown"
        )


class AdminTests(unittest.TestCase):
    def test_status_notes_are_admin(self):
        for text in (
            "Rebased https://x/commits/abc?since=def",
            "rebase https://x/commits/abc",
            "[rebased](https://x/commits/abc#core%2FFile.java)",
            "Rebased & resolved conflicts (https://x/commits/abc)",
            "same as https://x/overview?commentId=1",
            "same here https://x/overview?commentId=1",
            "depends on https://x/pull-requests/2",
            "related to https://x/pull-requests/250",
            "![image.png](attachment:7210/56520)",
            "https://x/overview?commentId=1045138",
        ):
            self.assertTrue(citext.is_admin(text), text)

    def test_review_remarks_are_not_admin(self):
        for text in (
            "this is always false now",
            "nit: rename this variable",
            "same problem here: the lock is never released",
            "why do we need a retry loop?",
        ):
            self.assertFalse(citext.is_admin(text), text)


class NoticeTests(unittest.TestCase):
    """prd/78: the deterministic no-fit filter, whole-text and conservative."""

    #: One example per family in the PRD table (plus the trailing-link and
    #: trailing-thanks shapes).
    TRUE_POSITIVES = (
        # rebase / merge / conflicts
        "Rebased https://x/commits/abc",
        "rebase",
        "[rebased](https://x/commits/abc#core%2FFile.java)",
        "Rebased & resolved conflicts (https://x/commits/abc)",
        "merged master",
        "Merge origin/main into feature",
        "resolved conflicts",
        "conflicts resolved in src/main.py",
        # review requests
        "please review",
        "PTAL",
        "can someone review this?",
        "@alice please review",
        "@alice check this",
        "LGTM",
        "+1",
        "ship it",
        "please review https://x/pull-requests/7 thanks",
        # author status
        "closing this",
        "done",
        "fixed in abc1234",
        "fixed in the next push",
        "added the test case",
        "updated the PR",
        "will add the fix",
        "I'll try another approach",
        "I’ll take a look",
        "nvm, handled",
        # link / attachment / admin
        "https://x/overview?commentId=1045138",
        "![image.png](attachment:7210/56520)",
        "same here https://x/overview?commentId=1",
        "fyi",
        "heads up",
        "see above",
        "thanks",
    )

    #: These are review remarks even though they open with a notice-ish word or
    #: share a family's vocabulary. A false positive here removes real text
    #: from the comment tax, which is the expensive error.
    FALSE_POSITIVES = (
        "can we also show the count in the message?",
        "@alice wdyt about the cache key?",
        "@alice thoughts?",
        "this will fail when the list is empty",
        "nit: rename this",
        "Rebased it, but this loop now skips the last element whenever the list "
        "is empty, so the diff will be wrong",
        "merged the two lists here",
        "review this loop for an overflow",
        "can you look at the whole pipeline? I think it will break",
        "why do we need a retry loop?",
        "same problem here: the lock is never released",
        "fixed in the loop above",
        "will change the behavior for existing callers",
        "will take the lock twice",
        "updated the count but the message still says the old value",
        "```\nrebased\n```",
        "see `rebase` above",
    )

    def test_true_positives_are_notices(self):
        for text in self.TRUE_POSITIVES:
            with self.subTest(text=text):
                self.assertTrue(citext.is_notice(text), text)

    def test_false_positives_are_review_remarks(self):
        for text in self.FALSE_POSITIVES:
            with self.subTest(text=text):
                self.assertFalse(citext.is_notice(text), text)

    def test_is_notice_supersedes_is_admin(self):
        for text in (
            "Rebased https://x/commits/abc",
            "same as https://x/overview?commentId=1",
            "depends on https://x/pull-requests/2",
            "related to https://x/pull-requests/250",
            "![image.png](attachment:7210/56520)",
            "https://x/overview?commentId=1045138",
        ):
            with self.subTest(text=text):
                self.assertTrue(citext.is_admin(text), text)
                self.assertTrue(citext.is_notice(text), text)


class BotRequestTests(unittest.TestCase):
    def test_alias_in_prose(self):
        self.assertTrue(citext.is_bot_request("@BOT review", ["bot"]))
        self.assertTrue(citext.is_bot_request("ping @bot wdyt?", ["bot"]))

    def test_annotation_email_and_code_are_not_mentions(self):
        self.assertFalse(citext.is_bot_request("@Override please", ["bot"]))
        self.assertFalse(citext.is_bot_request("mail a@bot.com", ["bot"]))
        self.assertFalse(citext.is_bot_request("see `@bot`", ["bot"]))

    def test_a_service_shaped_handle_needs_no_configuration(self):
        for text in ("@svc-review please look", "@ci-bot run", "@deploy-bot check"):
            self.assertTrue(citext.is_bot_request(text, []), text)
        self.assertTrue(citext.is_bot_request("@bot review", []))

    def test_a_human_alias_is_not_service_shaped(self):
        self.assertFalse(citext.is_bot_request("@APE review", []))


if __name__ == "__main__":
    unittest.main()
