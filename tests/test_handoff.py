from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout
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
    first_person_claims,
    publish_command,
)
from mailman.models import AgentConfig, RunRecord
from mailman.provenance import record_provenance


BODY = """Nothing was cached, so every call recomputed the window.

The fix caches it. 76 tests pass at 2.3.3.
"""

CLAIMING_BODY = BODY + "\nI have read, tested, and take responsibility for it.\n"


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


def _close_the_case(
    directory: Path, *, pull_request: int = 21961, superseded_by: int | None = 21967
) -> None:
    """Provenance as it reads after `mailman provenance --superseded-by`."""
    submission = directory / "submission"
    submission.mkdir(exist_ok=True)
    (submission / "provenance.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": directory.name,
                "repository": "python/mypy",
                "pull_request": pull_request,
                "state": "CLOSED",
                "superseded_by": superseded_by,
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    (submission / "submission.json").write_text(
        json.dumps({"target": "python/mypy", "issue_number": 21960}),
        encoding="utf-8",
        newline="\n",
    )


class ClosedRunTests(unittest.TestCase):
    """After python/mypy#21961 was superseded by #21967, a review comment went
    to #21967 in the same minute. Its author asked for the activity to stop.
    See https://github.com/wolfgang-aura/Mailman/issues/87."""

    def setUp(self):
        authors = patch(
            "mailman.handoff.check_authorship",
            return_value={"ok": True, "head": "fixture"},
        )
        authors.start()
        self.addCleanup(authors.stop)

    def _comment(self, root: Path, directory: Path, issue: int, **extra):
        body_path = root / "reply.md"
        body_path.write_text("Thanks, closing this one.\n", encoding="utf-8")
        return build_handoff(
            run_id=directory.name,
            run_directory=directory,
            body_path=body_path,
            kind="issue-comment",
            repository="python/mypy",
            issue_number=issue,
            **extra,
        )

    def test_a_comment_on_the_superseding_pull_request_is_refused(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            _close_the_case(directory)
            with self.assertRaises(ValueError) as caught:
                self._comment(root, directory, 21967)
            self.assertIn("superseded by #21967", str(caught.exception))
            self.assertIn("the superseding pull request #21967", str(caught.exception))
            self.assertIn("--closing-reply", str(caught.exception))

    def test_the_issue_and_our_own_pull_request_are_closed_too(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            _close_the_case(directory)
            for number in (21960, 21961):
                with self.assertRaises(ValueError, msg=number):
                    self._comment(root, directory, number)

    def test_a_closed_pull_request_without_a_successor_is_closed_as_well(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            _close_the_case(directory, superseded_by=None)
            with self.assertRaises(ValueError) as caught:
                self._comment(root, directory, 21961)
            self.assertIn("#21961 is closed", str(caught.exception))

    def test_an_unrelated_thread_is_not_gated(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            _close_the_case(directory)
            record, _ = self._comment(root, directory, 21999)
            self.assertFalse(record["closing_reply"])
            self.assertTrue(record["closure"]["closed"])

    def test_an_open_run_is_untouched(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            record, _ = self._comment(root, directory, 21967)
            self.assertFalse(record["closure"]["closed"])
            with self.assertRaises(ValueError):
                self._comment(root, directory, 21967, closing_reply=True)

    def test_one_closing_reply_is_allowed_and_a_second_thread_is_not(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            _close_the_case(directory)
            record, block = self._comment(root, directory, 21967, closing_reply=True)
            self.assertTrue(record["closing_reply"])
            self.assertIn("CLOSING REPLY", block)
            self.assertTrue(check_handoff(directory)["ok"])
            # Re-rendering the same reply is fine; the marker names the thread.
            self._comment(root, directory, 21967, closing_reply=True)
            with self.assertRaises(ValueError) as caught:
                self._comment(root, directory, 21960, closing_reply=True)
            self.assertIn("already went to #21967", str(caught.exception))

    def test_the_case_closing_after_the_handoff_stops_the_publish(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            record, _ = self._comment(root, directory, 21967)
            self.assertTrue(check_handoff(directory)["ok"])
            _close_the_case(directory)
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "run-closed")

    def test_the_cli_refuses_and_names_the_flag(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = _run_directory(root)
            _close_the_case(directory)
            body_path = root / "reply.md"
            body_path.write_text("Thanks.\n", encoding="utf-8")
            shared = [
                "handoff",
                run.run_id,
                "--body",
                str(body_path),
                "--repo",
                "python/mypy",
                "--kind",
                "issue-comment",
                "--issue",
                "21967",
                "--data-root",
                str(root),
            ]
            stream = io.StringIO()
            with redirect_stdout(stream):
                self.assertEqual(main(shared), 2)
            with redirect_stdout(stream):
                self.assertEqual(main([*shared, "--closing-reply"]), 0)
            self.assertIn("CLOSING REPLY", stream.getvalue())

    def test_provenance_says_the_case_is_closed_when_it_closes_it(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = _run_directory(root)
            (directory / "submission").mkdir()
            (directory / "submission" / "submission.json").write_text(
                json.dumps({"target": "pmorissette/ffn", "issue_number": 327}),
                encoding="utf-8",
            )
            out, err = io.StringIO(), io.StringIO()
            closed = {"available": True, "state": "CLOSED"}
            with patch(
                "mailman.cli.record_provenance",
                side_effect=lambda **kw: record_provenance(
                    **kw, state_lookup=lambda *_: closed
                ),
            ):
                with redirect_stdout(out), redirect_stderr(err):
                    code = main(
                        [
                            "provenance",
                            run.run_id,
                            "--pr",
                            "328",
                            "--superseded-by",
                            "330",
                            "--data-root",
                            str(root),
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertIn("case closed: our pull request was superseded by #330", err.getvalue())
            self.assertIn("the issue #327", err.getvalue())
            self.assertIn("--closing-reply", err.getvalue())
