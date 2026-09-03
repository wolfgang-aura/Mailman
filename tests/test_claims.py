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
    """Answer the two API paths `read_claims` asks for, and nothing else."""

    def __init__(self, issue: dict, comments: list[dict]) -> None:
        self.issue = issue
        self.comments = comments
        self.asked: list[str] = []

    def __call__(self, arguments, **keywords):
        path = arguments[-1]
        self.asked.append(path)
        payload = self.comments if "/comments" in path else self.issue
        return _Result(json.dumps(payload))


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
