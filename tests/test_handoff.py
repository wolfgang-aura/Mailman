from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from mailman.cli import main
from mailman.handoff import (
    HANDOFF_FILENAME,
    body_digest,
    build_handoff,
    check_handoff,
    check_prior_art_freshness,
    draft_leftovers,
    first_person_claims,
    publish_command,
)
from mailman.models import AgentConfig, RunRecord


BODY = """Nothing was cached, so every call recomputed the window.

The fix caches it. 76 tests pass at 2.3.3.
"""

CLAIMING_BODY = BODY + "\nI have read, tested, and take responsibility for it.\n"

# The shape pytest-dev/pytest#14993 was filed in: a body written from
# `pull-request.md` with the draft's own furniture still in it.
LEFTOVER_BODY = """Closes #14992.

The traversal put dependencies ahead of the initial request order.

### An alternative I did not take

_Name the design you rejected and the trade-off._

### AI disclosure

This change was drafted with AI assistance. Human review and filing remain
pending.

## Before filing

- [ ] The base commit is level with the target's default branch

Evidence is under .mailman/20260908T230139Z-abc123/verification.
"""


def _run_directory(root: Path) -> tuple[RunRecord, Path]:
    run = RunRecord(
        run_id="20260904T000000Z-aaaaaa",
        repository="pmorissette/ffn",
        issue="pmorissette/ffn#327",
        base_commit="0123456789abcdef",
        primary=AgentConfig(agent="claude", model="claude-opus-5"),
        reviewer=AgentConfig(agent="codex", model="gpt-5"),
    )
    directory = root / run.run_id
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(
        json.dumps(run.to_dict()), encoding="utf-8", newline="\n"
    )
    return run, directory


def _prior_art(
    directory: Path,
    *,
    search_age_minutes: float = 1,
    claims_age_minutes: float = 1,
    repository: str = "pmorissette/ffn",
    success: bool = True,
    self_reported: bool = False,
) -> None:
    """Write the prior-art evidence a publishable run carries."""
    now = datetime.now(UTC)
    (directory / "duplicate-search.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "searched_at": (
                    now - timedelta(minutes=search_age_minutes)
                ).isoformat(),
                "repository": repository,
                "query": "rolling window cache",
                "success": success,
                "matches": [],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    (directory / "claims.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "collected_at": (
                    now - timedelta(minutes=claims_age_minutes)
                ).isoformat(),
                "success": True,
                "self_reported": self_reported,
                "claims": [],
            }
        ),
        encoding="utf-8",
        newline="\n",
    )


class DigestTests(unittest.TestCase):
    def test_line_endings_and_edge_whitespace_do_not_change_the_digest(self) -> None:
        self.assertEqual(
            body_digest("one\ntwo\n"), body_digest("one\r\ntwo\r\n\r\n")
        )

    def test_a_changed_word_changes_the_digest(self) -> None:
        self.assertNotEqual(body_digest("76 tests pass"), body_digest("77 tests pass"))


class FirstPersonTests(unittest.TestCase):
    def test_a_read_and_tested_claim_is_reported_with_its_line(self) -> None:
        claims = first_person_claims(CLAIMING_BODY)
        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["line"], 5)
        self.assertIn("take responsibility", claims[0]["text"])

    def test_a_body_that_claims_nothing_is_clean(self) -> None:
        self.assertEqual(first_person_claims(BODY), [])

    def test_the_harness_reporting_its_own_run_is_not_a_first_person_claim(
        self,
    ) -> None:
        self.assertEqual(
            first_person_claims("The harness ran the suite and it passed."), []
        )


class DraftLeftoverTests(unittest.TestCase):
    def test_a_body_written_from_the_draft_is_reported_line_by_line(self) -> None:
        codes = {item["code"] for item in draft_leftovers(LEFTOVER_BODY)}
        self.assertEqual(
            codes, {"scaffolding", "pending-state", "internal-reference"}
        )

    def test_the_pending_sentence_is_caught_where_it_wraps(self) -> None:
        found = draft_leftovers(LEFTOVER_BODY)
        pending = [item for item in found if item["code"] == "pending-state"]
        self.assertEqual(len(pending), 1)
        self.assertIn("pending", pending[0]["text"])

    def test_a_run_directory_path_is_internal_and_a_commit_is_not(self) -> None:
        self.assertEqual(
            [item["code"] for item in draft_leftovers("Evidence: .mailman/runs/x")],
            ["internal-reference"],
        )
        self.assertEqual(draft_leftovers("Fixed at 3fd8675d6d79."), [])

    def test_a_body_a_maintainer_would_want_is_clean(self) -> None:
        self.assertEqual(draft_leftovers(BODY), [])

    def test_naming_the_harness_that_ran_the_tests_is_not_a_leftover(self) -> None:
        self.assertEqual(
            draft_leftovers(
                "The Mailman harness executed the commands, not the agent."
            ),
            [],
        )


class PublishCommandTests(unittest.TestCase):
    def test_a_pull_request_reads_the_body_from_the_hashed_file(self) -> None:
        command = publish_command(
            kind="pull-request",
            body_path=Path("/tmp/body.md"),
            repository="pmorissette/ffn",
            title="Cache the rolling window",
            head="Mailman-Fork:mailman/run-1",
            base="master",
        )
        self.assertIn("--body-file", command)
        self.assertNotIn("--body ", command)
        self.assertIn("--head Mailman-Fork:mailman/run-1", command)

    def test_a_pull_request_without_a_head_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            publish_command(
                kind="pull-request",
                body_path=Path("/tmp/body.md"),
                repository="pmorissette/ffn",
                title="t",
                base="master",
            )

    def test_an_issue_comment_needs_a_number(self) -> None:
        with self.assertRaises(ValueError):
            publish_command(
                kind="issue-comment",
                body_path=Path("/tmp/body.md"),
                repository="pmorissette/ffn",
            )


class BuildHandoffTests(unittest.TestCase):

    def setUp(self):
        authors = patch("mailman.handoff.check_authorship", return_value={"ok": True, "head": "fixture"})
        authors.start()
        self.addCleanup(authors.stop)

    def test_the_body_and_the_command_arrive_in_one_block(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = _run_directory(root)
            body_path = root / "body.md"
            body_path.write_text(BODY, encoding="utf-8", newline="\n")
            record, block = build_handoff(
                run_id=run.run_id,
                run_directory=directory,
                body_path=body_path,
                kind="pull-request",
                repository="pmorissette/ffn",
                title="Cache the rolling window",
                head="Mailman-Fork:mailman/run-1",
                base="master",
            )
            self.assertIn("Nothing was cached", block)
            self.assertIn("gh pr create", block)
            self.assertLess(block.index("Nothing was cached"), block.index("gh pr create"))
            self.assertIn(record["verify_command"], block)
            self.assertTrue((directory / HANDOFF_FILENAME).is_file())

    def test_a_body_that_is_not_utf8_is_refused_rather_than_repaired(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = _run_directory(root)
            body_path = root / "body.md"
            # An em dash saved by PowerShell's default encoding, not UTF-8.
            body_path.write_bytes(b"Reproducer \x97 before the fix\n")
            with self.assertRaises(ValueError) as caught:
                build_handoff(
                    run_id=run.run_id,
                    run_directory=directory,
                    body_path=body_path,
                    kind="pull-request",
                    repository="pmorissette/ffn",
                    title="t",
                    head="Mailman-Fork:b",
                    base="master",
                )
            self.assertIn("UTF-8", str(caught.exception))

    def test_an_empty_body_is_refused(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = _run_directory(root)
            body_path = root / "body.md"
            body_path.write_text("\n", encoding="utf-8", newline="\n")
            with self.assertRaises(ValueError):
                build_handoff(
                    run_id=run.run_id,
                    run_directory=directory,
                    body_path=body_path,
                    kind="pull-request",
                    repository="pmorissette/ffn",
                    title="t",
                    head="Mailman-Fork:b",
                    base="master",
                )


class CheckHandoffTests(unittest.TestCase):

    def setUp(self):
        authors = patch("mailman.handoff.check_authorship", return_value={"ok": True, "head": "fixture"})
        authors.start()
        self.addCleanup(authors.stop)

    def _prepared(self, root: Path, body: str) -> tuple[Path, Path]:
        run, directory = _run_directory(root)
        body_path = root / "body.md"
        body_path.write_text(body, encoding="utf-8", newline="\n")
        build_handoff(
            run_id=run.run_id,
            run_directory=directory,
            body_path=body_path,
            kind="pull-request",
            repository="pmorissette/ffn",
            title="Cache the rolling window",
            head="Mailman-Fork:mailman/run-1",
            base="master",
        )
        _prior_art(directory)
        return directory, body_path

    def test_an_unchanged_body_passes(self) -> None:
        with TemporaryDirectory() as name:
            directory, _ = self._prepared(Path(name), BODY)
            self.assertTrue(check_handoff(directory)["ok"])

    def test_an_edit_after_the_preview_blocks(self) -> None:
        with TemporaryDirectory() as name:
            directory, body_path = self._prepared(Path(name), BODY)
            body_path.write_text(BODY + "\nOne more line.\n", encoding="utf-8")
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "body-changed")

    def test_a_run_that_never_previewed_blocks(self) -> None:
        with TemporaryDirectory() as name:
            _, directory = _run_directory(Path(name))
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "no-handoff")


class HandoffCliTests(unittest.TestCase):

    def setUp(self):
        authors = patch("mailman.handoff.check_authorship", return_value={"ok": True, "head": "fixture"})
        authors.start()
        self.addCleanup(authors.stop)

    def _invoke(self, arguments: list[str]) -> tuple[int, str]:
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(arguments)
        return code, stream.getvalue()

    def test_a_first_person_claim_exits_non_zero_and_names_the_line(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, _ = _run_directory(root)
            body_path = root / "body.md"
            body_path.write_text(CLAIMING_BODY, encoding="utf-8", newline="\n")
            code, output = self._invoke(
                [
                    "handoff",
                    run.run_id,
                    "--body",
                    str(body_path),
                    "--repo",
                    "pmorissette/ffn",
                    "--head",
                    "Mailman-Fork:mailman/run-1",
                    "--base",
                    "master",
                    "--title",
                    "Cache the rolling window",
                    "--data-root",
                    str(root),
                ]
            )
            self.assertEqual(code, 1)
            self.assertIn("FIRST-PERSON CLAIMS", output)
            self.assertIn("take responsibility", output)

    def test_draft_scaffolding_exits_non_zero_and_names_the_line(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, _ = _run_directory(root)
            body_path = root / "body.md"
            body_path.write_text(LEFTOVER_BODY, encoding="utf-8", newline="\n")
            code, output = self._invoke(
                [
                    "handoff",
                    run.run_id,
                    "--body",
                    str(body_path),
                    "--repo",
                    "pytest-dev/pytest",
                    "--head",
                    "Mailman-Fork:mailman/issue-14992",
                    "--base",
                    "main",
                    "--title",
                    "Keep usefixtures fixtures ahead of autouse dependencies",
                    "--data-root",
                    str(root),
                ]
            )
            self.assertEqual(code, 1)
            self.assertIn("DRAFT LEFTOVERS", output)
            self.assertIn("remain", output)

    def test_handoff_check_refuses_a_body_that_still_carries_the_draft(
        self,
    ) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = _run_directory(root)
            body_path = root / "body.md"
            body_path.write_text(LEFTOVER_BODY, encoding="utf-8", newline="\n")
            _prior_art(directory)
            self._invoke(
                [
                    "handoff",
                    run.run_id,
                    "--body",
                    str(body_path),
                    "--repo",
                    "pytest-dev/pytest",
                    "--head",
                    "Mailman-Fork:mailman/issue-14992",
                    "--base",
                    "main",
                    "--title",
                    "Keep usefixtures fixtures ahead",
                    "--data-root",
                    str(root),
                ]
            )
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "draft-leftovers")
            self.assertIn("pending-state", result["detail"])

    def test_handoff_check_exits_non_zero_once_the_body_changes(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, _ = _run_directory(root)
            body_path = root / "body.md"
            body_path.write_text(BODY, encoding="utf-8", newline="\n")
            _prior_art(root / run.run_id)
            shared = [
                run.run_id,
                "--body",
                str(body_path),
                "--repo",
                "pmorissette/ffn",
                "--head",
                "Mailman-Fork:mailman/run-1",
                "--base",
                "master",
                "--title",
                "Cache the rolling window",
                "--data-root",
                str(root),
            ]
            code, _ = self._invoke(["handoff", *shared])
            self.assertEqual(code, 0)
            code, _ = self._invoke(
                ["handoff-check", run.run_id, "--data-root", str(root)]
            )
            self.assertEqual(code, 0)
            body_path.write_text(BODY + "\nEdited later.\n", encoding="utf-8")
            code, output = self._invoke(
                ["handoff-check", run.run_id, "--data-root", str(root)]
            )
            self.assertEqual(code, 1)
            self.assertIn("body-changed", output)


class PriorArtFreshnessTests(unittest.TestCase):

    def setUp(self):
        authors = patch("mailman.handoff.check_authorship", return_value={"ok": True, "head": "fixture"})
        authors.start()
        self.addCleanup(authors.stop)

    """The push-time half of the duplicate check.

    Run 20260903T052426Z-ad8196 finished clean against `encode/starlette` and
    was overtaken by a byte-identical pull request 94 minutes later. The search
    that cleared it was hours old by the time anyone would have published.
    See https://github.com/wolfgang-aura/Mailman/issues/41.
    """

    def _prepared(self, root: Path, **evidence: object) -> Path:
        run, directory = _run_directory(root)
        body_path = root / "body.md"
        body_path.write_text(BODY, encoding="utf-8", newline="\n")
        build_handoff(
            run_id=run.run_id,
            run_directory=directory,
            body_path=body_path,
            kind="pull-request",
            repository="pmorissette/ffn",
            title="Cache the rolling window",
            head="Mailman-Fork:mailman/run-1",
            base="master",
        )
        if evidence.pop("prior_art", True):
            _prior_art(directory, **evidence)  # type: ignore[arg-type]
        return directory

    def test_evidence_from_minutes_ago_publishes(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name))
            result = check_handoff(directory)
            self.assertTrue(result["ok"])
            self.assertEqual(result["prior_art"]["reason"], "fresh")

    def test_a_search_from_yesterday_refuses(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name), search_age_minutes=26 * 60)
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "duplicate-search-stale")
            self.assertIn("duplicate-search", result["detail"])

    def test_a_run_that_never_searched_refuses(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name), prior_art=False)
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "no-duplicate-search")

    def test_a_search_that_failed_clears_nothing(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name), success=False)
            self.assertEqual(
                check_handoff(directory)["reason"], "duplicate-search-failed"
            )

    def test_a_search_of_another_repository_does_not_count(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name), repository="encode/starlette")
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "duplicate-search-elsewhere")

    def test_a_stale_claims_check_refuses_too(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name), claims_age_minutes=26 * 60)
            self.assertEqual(check_handoff(directory)["reason"], "claims-stale")

    def test_a_self_reported_defect_has_no_thread_to_be_claimed_in(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(
                Path(name), claims_age_minutes=26 * 60, self_reported=True
            )
            self.assertTrue(check_handoff(directory)["ok"])

    def test_an_issue_comment_is_not_a_submission_and_is_not_gated(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = _run_directory(root)
            body_path = root / "body.md"
            body_path.write_text(BODY, encoding="utf-8", newline="\n")
            build_handoff(
                run_id=run.run_id,
                run_directory=directory,
                body_path=body_path,
                kind="issue-comment",
                repository="pmorissette/ffn",
                issue_number=327,
            )
            self.assertTrue(check_handoff(directory)["ok"])

    def test_the_age_of_the_evidence_is_reported_with_the_refusal(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name), search_age_minutes=180)
            result = check_prior_art_freshness(
                directory, repository="pmorissette/ffn"
            )
            self.assertAlmostEqual(
                result["evidence"]["duplicate_search_age_minutes"], 180, delta=1
            )
            self.assertEqual(result["evidence"]["max_age_minutes"], 60)


if __name__ == "__main__":
    unittest.main()
