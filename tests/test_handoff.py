from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from mailman.cli import main
from mailman.handoff import (
    HANDOFF_FILENAME,
    body_digest,
    build_handoff,
    check_handoff,
    first_person_claims,
    publish_command,
)
from mailman.models import AgentConfig, RunRecord


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


if __name__ == "__main__":
    unittest.main()
