"""The two gates added after pmorissette/ffn#328 was closed and corrected.

Fork ownership, because a maintainer could not push to an organisation-owned
fork and closed the pull request instead. Preservation claims, because the body
said ndarray input was unchanged when it had never worked.
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from mailman.handoff import (
    build_handoff,
    check_handoff,
    head_owner,
    maintainer_edit_warning,
    preservation_claims,
)
from mailman.models import AgentConfig, RunRecord


def _fresh_prior_art(directory: Path) -> None:
    """The prior-art evidence `check_handoff` requires before a pull request."""
    now = datetime.now(UTC).isoformat()
    (directory / "duplicate-search.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "searched_at": now,
                "repository": "pmorissette/ffn",
                "query": "scalar path",
                "success": True,
                "matches": [],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    (directory / "claims.json").write_text(
        json.dumps(
            {"schema_version": 1, "collected_at": now, "success": True, "claims": []}
        ),
        encoding="utf-8",
        newline="\n",
    )


def _never_asked(owner: str) -> str | None:
    return None


def _organisation(owner: str) -> str | None:
    return "Organization"


def _user(owner: str) -> str | None:
    return "User"


class HeadOwnerTests(unittest.TestCase):
    def test_the_owner_is_the_half_before_the_colon(self) -> None:
        self.assertEqual(head_owner("Mailman-Fork:mailman/run-1"), "Mailman-Fork")

    def test_a_bare_branch_names_no_owner(self) -> None:
        self.assertIsNone(head_owner("mailman/run-1"))
        self.assertIsNone(head_owner(None))
        self.assertIsNone(head_owner(":branch"))


class MaintainerEditWarningTests(unittest.TestCase):
    def test_an_organisation_fork_is_warned_about(self) -> None:
        warning = maintainer_edit_warning("Mailman-Fork", "Organization")
        assert warning is not None
        self.assertIn("organisation", warning)
        self.assertIn("user account", warning)

    def test_a_user_fork_is_silent(self) -> None:
        self.assertIsNone(maintainer_edit_warning("wolfgang-aura", "User"))

    def test_an_unanswered_lookup_is_not_a_pass(self) -> None:
        warning = maintainer_edit_warning("wolfgang-aura", None)
        assert warning is not None
        self.assertIn("could not determine", warning)

    def test_no_head_owner_means_nothing_to_warn_about(self) -> None:
        self.assertIsNone(maintainer_edit_warning(None, None))


class PreservationClaimTests(unittest.TestCase):
    def test_a_lead_in_carries_its_bullets(self) -> None:
        body = (
            "## Change\n"
            "\n"
            "Existing behavior is unchanged for:\n"
            "\n"
            "- Series returns with a Series benchmark\n"
            "- Scalar and ndarray inputs\n"
            "\n"
            "Unrelated paragraph.\n"
        )
        claims = preservation_claims(body)
        texts = [claim["text"] for claim in claims]
        self.assertIn("Existing behavior is unchanged for:", texts)
        self.assertIn("- Scalar and ndarray inputs", texts)
        items = [claim for claim in claims if claim["under"]]
        self.assertEqual({claim["under"] for claim in items}, {3})
        self.assertNotIn("Unrelated paragraph.", texts)

    def test_a_standalone_assertion_is_caught(self) -> None:
        claims = preservation_claims("This shape was not broken by the fix.\n")
        self.assertEqual(len(claims), 1)
        self.assertIsNone(claims[0]["under"])

    def test_a_body_claiming_nothing_produces_nothing(self) -> None:
        body = "## Summary\n\nAlign the benchmark on the index.\n"
        self.assertEqual(preservation_claims(body), [])

    def test_a_list_that_follows_no_lead_in_is_left_alone(self) -> None:
        body = "## Scope\n\n- Frequency inference\n- Packaging\n"
        self.assertEqual(preservation_claims(body), [])


class BuildHandoffTests(unittest.TestCase):

    def setUp(self):
        authors = patch("mailman.handoff.check_authorship", return_value={"ok": True, "head": "fixture"})
        authors.start()
        self.addCleanup(authors.stop)
        foreign = patch("mailman.handoff.foreign_pull_request", return_value=None)
        foreign.start()
        self.addCleanup(foreign.stop)

    def _run(self, root: Path):
        run = RunRecord(
            run_id="20260906T000000Z-bbbbbb",
            repository="pmorissette/ffn",
            issue="pmorissette/ffn#327",
            base_commit="0123456789abcdef",
            primary=AgentConfig(agent="claude", model="claude-opus-5"),
            reviewer=AgentConfig(agent="codex", model="gpt-5"),
        )
        directory = root / run.run_id
        directory.mkdir(parents=True)
        return run, directory

    def test_an_organisation_head_reaches_the_printed_block(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = self._run(root)
            body_path = directory / "body.md"
            body_path.write_text(
                "Aligns the benchmark. Existing behavior is unchanged for:\n"
                "\n"
                "- ndarray inputs\n",
                encoding="utf-8",
            )
            record, block = build_handoff(
                run_id=run.run_id,
                run_directory=directory,
                body_path=body_path,
                kind="pull-request",
                repository="pmorissette/ffn",
                title="Align the benchmark",
                head="Mailman-Fork:mailman/run-1",
                base="master",
                owner_type_lookup=_organisation,
            )
            self.assertEqual(record["head_owner"], "Mailman-Fork")
            self.assertEqual(record["head_owner_type"], "Organization")
            self.assertIn("FORK OWNERSHIP", block)
            self.assertIn("PRESERVATION CLAIMS", block)
            self.assertIn("ndarray inputs", block)
            # The warnings come before the command that publishes.
            self.assertLess(block.index("FORK OWNERSHIP"), block.index("gh pr create"))

    def test_a_user_head_with_no_claims_prints_neither_block(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = self._run(root)
            body_path = directory / "body.md"
            body_path.write_text("Align the benchmark on the index.\n", encoding="utf-8")
            record, block = build_handoff(
                run_id=run.run_id,
                run_directory=directory,
                body_path=body_path,
                kind="pull-request",
                repository="pmorissette/ffn",
                title="Align the benchmark",
                head="wolfgang-aura:mailman/run-1",
                base="master",
                owner_type_lookup=_user,
            )
            self.assertIsNone(record["maintainer_edit_warning"])
            self.assertEqual(record["preservation_claims"], [])
            self.assertNotIn("FORK OWNERSHIP", block)
            self.assertNotIn("PRESERVATION CLAIMS", block)

    def test_an_issue_comment_asks_nobody_about_forks(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = self._run(root)
            body_path = directory / "body.md"
            body_path.write_text("Thanks for the correction.\n", encoding="utf-8")
            record, _ = build_handoff(
                run_id=run.run_id,
                run_directory=directory,
                body_path=body_path,
                kind="issue-comment",
                repository="pmorissette/ffn",
                issue_number=330,
                owner_type_lookup=_never_asked,
            )
            self.assertIsNone(record["head_owner"])
            self.assertIsNone(record["maintainer_edit_warning"])

    def test_the_check_reports_what_the_preview_showed(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = self._run(root)
            body_path = directory / "body.md"
            body_path.write_text(
                "The scalar path is unchanged.\n", encoding="utf-8"
            )
            build_handoff(
                run_id=run.run_id,
                run_directory=directory,
                body_path=body_path,
                kind="pull-request",
                repository="pmorissette/ffn",
                title="t",
                head="Mailman-Fork:b",
                base="master",
                owner_type_lookup=_organisation,
            )
            _fresh_prior_art(directory)
            result = check_handoff(directory)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["preservation_claims"]), 1)
            self.assertIn("organisation", result["maintainer_edit_warning"])
            stored = json.loads((directory / "handoff.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["head_owner_type"], "Organization")


if __name__ == "__main__":
    unittest.main()
