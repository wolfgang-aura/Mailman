"""A claim made in an issue's own comments is prior art the gate must see.

`openai/openai-agents-python` #4775 had no pull request against it, so the
duplicate search was empty and `check-target` called it unclaimed. Its second
comment was "I'd like to work on this issue", followed by a scope plan. See
https://github.com/wolfgang-aura/Mailman/issues/36.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.claims import (
    CLAIMS_FILENAME,
    classify_comment,
    load_claims,
    pull_request_references,
    read_claims,
    render_claims,
)


def _comment(body: str, *, association: str = "NONE", login: str = "someone") -> dict:
    return {
        "body": body,
        "author_association": association,
        "created_at": "2026-09-01T00:00:00Z",
        "user": {"login": login, "type": "User"},
    }


class _Result:
    def __init__(self, stdout: str, exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code
        self.timed_out = False

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code, "timed_out": self.timed_out}


def _failing(arguments, **keywords):
    return _Result("", exit_code=1)


class _FakeGh:
    """Answer the three API paths `read_claims` asks for, and nothing else."""

    def __init__(
        self,
        issue: dict,
        comments: list[dict],
        timeline: list[dict] | None = None,
    ) -> None:
        self.issue = issue
        self.comments = comments
        self.timeline = timeline or []
        self.asked: list[str] = []

    def __call__(self, arguments, **keywords):
        # `[gh, "api", PATH, ...query fields]`: the path is the third word,
        # never the last one.
        path = arguments[2]
        self.asked.append(path)
        if "/comments" in path:
            payload: object = self.comments
        elif "/timeline" in path:
            payload = self.timeline
        else:
            payload = self.issue
        return _Result(json.dumps(payload))


def _cross_reference(url: str) -> dict:
    return {"event": "cross-referenced", "source": {"issue": {"html_url": url}}}


class ClassifyCommentTests(unittest.TestCase):
    def test_the_openai_agents_comment_reads_as_a_claim(self) -> None:
        self.assertEqual(
            classify_comment(_comment("I'd like to work on this issue.")), "claim"
        )

    def test_the_comment_as_github_actually_stored_it_reads_as_a_claim(self) -> None:
        # The live comment on #4775 carries U+2019, not an ASCII apostrophe,
        # which is what the first version of this classifier missed.
        self.assertEqual(
            classify_comment(
                _comment(
                    "I’d like to work on this issue.\n\nPlanned scope:\n"
                    "- route resumed pending-input Session appends through the "
                    "existing fail-closed checkpoint;",
                    association="CONTRIBUTOR",
                )
            ),
            "claim",
        )

    def test_the_common_claim_phrasings_read_as_claims(self) -> None:
        for body in (
            "I'm working on this",
            "I am currently working on it",
            "I'll take this one",
            "I can take a look and open a PR",
            "please assign this to me",
            "assign me",
            "Can I work on this?",
            "picking this up now",
            "On it!",
            "let me take a stab at this",
            "I have a patch ready for this",
            "Taking this up, will send a PR shortly",
            "I would like to contribute a fix here",
            # domokane/FinancePy#266, #262, #267, #264: a reporter who has
            # the fix and offers it. See wolfgang-aura/Mailman#93.
            "I would be glad to prepare a focused PR and discuss a contract.",
            "whether the attached candidate is suitable for a PR?",
            "Proposed correction in `financepy/products/bonds/bond_zero.py`:",
            "I would be happy to help take this from a report to a fix",
        ):
            with self.subTest(body=body):
                self.assertEqual(classify_comment(_comment(body)), "claim")

    def test_a_question_about_the_bug_is_not_a_claim(self) -> None:
        for body in (
            "Is anyone working on this?",
            "Any update on this issue?",
            "What version of Python are you on?",
            "I can reproduce this on 3.12 as well",
            "This also breaks for me",
            "PRs welcome",
            "Thanks for the report!",
            "Has this been fixed already?",
            "I am not working on this anymore",
        ):
            with self.subTest(body=body):
                self.assertIsNone(classify_comment(_comment(body)))

    def test_a_maintainer_handing_the_work_over_reads_as_an_assignment(self) -> None:
        for body in (
            "Assigned to you, thanks!",
            "I've assigned this to @someone",
            "Go ahead, all yours",
            "Feel free to open a PR",
            "You can take this one",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    classify_comment(_comment(body, association="MEMBER")),
                    "assignment",
                )

    def test_the_same_words_from_an_outsider_are_not_an_assignment(self) -> None:
        # Only somebody who can actually hand out the work is handing it out.
        self.assertIsNone(classify_comment(_comment("Go ahead, all yours")))

    def test_a_bot_comment_is_never_a_claim(self) -> None:
        comment = _comment("I'm working on this", login="github-actions[bot]")
        comment["user"]["type"] = "Bot"

        self.assertIsNone(classify_comment(comment))


class ReadClaimsTests(unittest.TestCase):
    def _run(self, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "issue.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "reference": {
                        "owner": "openai",
                        "repository": "openai-agents-python",
                        "number": 4775,
                        "url": (
                            "https://github.com/openai/openai-agents-python"
                            "/issues/4775"
                        ),
                    },
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_a_claim_in_the_comments_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": []},
                    [
                        _comment("Thanks for the report!", association="MEMBER"),
                        _comment("I'd like to work on this issue."),
                    ],
                ),
            )
            # Read back inside the block: the record is on disk only as long
            # as the temporary directory is.
            self.assertEqual(load_claims(root), record)
            self.assertTrue((root / CLAIMS_FILENAME).is_file())

        self.assertTrue(record["success"])
        self.assertEqual(record["repository"], "openai/openai-agents-python")
        self.assertEqual(record["issue_number"], 4775)
        self.assertEqual(record["comments_read"], 2)
        self.assertEqual(len(record["claims"]), 1)
        self.assertEqual(record["claims"][0]["author"], "someone")
        self.assertEqual(record["assignments"], [])
        self.assertEqual(record["assignees"], [])
        self.assertIn("work on this issue", record["claims"][0]["quote"])

    def test_a_claim_in_the_issue_body_is_recorded(self) -> None:
        # domokane/FinancePy#267: the report carries the diff and nobody has
        # commented, and the reporter has still claimed the work.
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {
                        "number": 267,
                        "assignees": [],
                        "user": {"login": "reporter"},
                        "author_association": "CONTRIBUTOR",
                        "body": (
                            "Proposed correction in `bond_zero.py`: "
                            "```diff -  md = dd / fp * 10000 +  md = dd / fp```"
                        ),
                    },
                    [],
                ),
            )

        self.assertTrue(record["success"])
        self.assertEqual(record["comments_read"], 0)
        self.assertEqual(len(record["claims"]), 1)
        self.assertEqual(record["claims"][0]["author"], "reporter")
        self.assertIn("Proposed correction", record["claims"][0]["quote"])

    def test_an_assigned_issue_records_its_assignee(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": [{"login": "maintainer"}]}, []
                ),
            )

        self.assertTrue(record["success"])
        self.assertEqual(record["assignees"], ["maintainer"])

    def test_a_maintainer_reply_is_recorded_as_an_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": []},
                    [
                        _comment("I'd like to work on this."),
                        _comment("Go ahead, all yours", association="MEMBER"),
                    ],
                ),
            )

        self.assertEqual(len(record["claims"]), 1)
        self.assertEqual(len(record["assignments"]), 1)
        self.assertEqual(record["assignments"][0]["author"], "someone")

    def test_a_clean_issue_records_no_claim_and_still_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": []},
                    [_comment("I can reproduce this on 3.12 as well")],
                ),
            )

        self.assertTrue(record["success"])
        self.assertEqual(record["claims"], [])
        self.assertEqual(record["comments_read"], 1)
        self.assertIn("no claim", render_claims(record).lower())

    def test_an_unreadable_issue_does_not_pass_as_a_clean_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(root, executable="gh", execute=_failing)

        self.assertFalse(record["success"])
        self.assertIn("detail", record)

    def test_a_run_with_no_captured_issue_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = read_claims(Path(temporary), executable="gh", execute=_failing)

        self.assertFalse(record["success"])
        self.assertIn("issue", record["detail"])

    def test_the_rendered_record_names_the_claimant_and_the_quote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": []},
                    [_comment("I'd like to work on this issue.")],
                ),
            )
        rendered = render_claims(record)

        self.assertIn("someone", rendered)
        self.assertIn("work on this issue", rendered)


if __name__ == "__main__":
    unittest.main()


class TriageFieldsTests(unittest.TestCase):
    """What the record says about who reported the issue and who answered.
    See https://github.com/wolfgang-aura/Mailman/issues/88."""

    def _run(self, root: Path) -> Path:
        return ReadClaimsTests._run(self, root)

    def test_an_outside_report_with_no_maintainer_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {
                        "number": 4775,
                        "assignees": [],
                        "state": "open",
                        "closed_at": None,
                        "author_association": "NONE",
                    },
                    [_comment("Reverified, still reproduces.")],
                ),
            )
            self.assertEqual(record["reporter_association"], "NONE")
            self.assertFalse(record["maintainer_replied"])
            self.assertIsNone(record["issue_closed_at"])

    def test_a_member_reply_and_a_close_are_both_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {
                        "number": 4775,
                        "assignees": [],
                        "state": "closed",
                        "closed_at": "2026-09-08T09:27:46Z",
                        "author_association": "NONE",
                    },
                    [_comment("Fixed in dc4e314.", association="OWNER")],
                ),
            )
            self.assertTrue(record["maintainer_replied"])
            self.assertEqual(record["issue_state"], "closed")
            self.assertEqual(record["issue_closed_at"], "2026-09-08T09:27:46Z")


class PullRequestReferenceTests(unittest.TestCase):
    """Every way a thread names the pull request that already fixes the issue.

    https://github.com/wolfgang-aura/Mailman/issues/98
    """

    def test_reads_a_bare_number_a_slug_and_a_url(self) -> None:
        found = pull_request_references(
            [
                "Draft implementation: "
                "[#12775](https://github.com/deepset-ai/haystack/pull/12775)",
                "See also getsentry/sentry-python#4001 and #22722.",
            ],
            repository="deepset-ai/haystack",
            exclude=[("deepset-ai/haystack", 12777)],
        )

        self.assertEqual(
            [(row["repository"], row["number"]) for row in found],
            [
                ("deepset-ai/haystack", 12775),
                ("getsentry/sentry-python", 4001),
                ("deepset-ai/haystack", 22722),
            ],
        )
        self.assertIn("12775", found[0]["text"])

    def test_the_issues_own_number_is_never_resolved(self) -> None:
        found = pull_request_references(
            ["This is a duplicate of #1497, filed as #1497 again."],
            repository="python-jsonschema/jsonschema",
            exclude=[("python-jsonschema/jsonschema", 1497)],
        )

        self.assertEqual(found, [])

    def test_the_same_pull_request_named_twice_is_one_reference(self) -> None:
        found = pull_request_references(
            [
                "Fixed by #22722.",
                "PR #22722 was merged on 2026-08-05.",
                "https://github.com/PrefectHQ/prefect/pull/22722",
            ],
            repository="PrefectHQ/prefect",
        )

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["number"], 22722)

    def test_the_count_is_capped_because_each_reference_is_a_gh_call(self) -> None:
        body = " ".join(f"#{index}" for index in range(100, 130))
        self.assertEqual(len(pull_request_references([body], repository="a/b")), 10)


class ReferenceRecordTests(unittest.TestCase):
    """What `read_claims` writes down for `prescreen` to resolve."""

    def _run(self, root: Path) -> Path:
        return ReadClaimsTests._run(self, root)

    def test_the_body_and_the_comments_are_both_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {
                        "number": 4775,
                        "assignees": [],
                        "body": "Draft implementation: #4800",
                    },
                    [_comment("Also fixed by openai/openai-python#900.")],
                ),
            )

        self.assertEqual(
            [(row["repository"], row["number"]) for row in record["references"]],
            [
                ("openai/openai-agents-python", 4800),
                ("openai/openai-python", 900),
            ],
        )

    def test_a_cross_referenced_pull_request_is_read_off_the_timeline(self) -> None:
        # python-jsonschema/jsonschema#1497: the one comment says "the
        # issue/PR in referencing" and names no number. GitHub linked it.
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": [], "body": "No numbers here."},
                    [_comment("Please look at the issue/PR in referencing.")],
                    [
                        _cross_reference(
                            "https://github.com/python-jsonschema/referencing/issues/366"
                        ),
                        _cross_reference(
                            "https://github.com/python-jsonschema/referencing/pull/367"
                        ),
                    ],
                ),
            )

        self.assertIn(
            ("python-jsonschema/referencing", 367),
            [(row["repository"], row["number"]) for row in record["references"]],
        )
