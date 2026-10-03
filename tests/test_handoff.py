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
    OFFER_HANDOFF_FILENAME,
    body_digest,
    build_handoff,
    check_handoff,
    check_prior_art_freshness,
    first_person_claims,
    load_handoff,
    load_offer_handoff,
    publish_command,
    unsourced_specification_claims,
)
from mailman.models import AgentConfig, RunRecord
from mailman.provenance import record_provenance
from mailman.touched_tests import diff_sha256


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
    complete: bool = True,
    matches: list[dict[str, object]] | None = None,
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
                "issue_number": 327,
                "success": success,
                "complete": complete,
                "matches": matches or [],
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
    _touched_tests(directory)


def _touched_tests(
    directory: Path, *, diff: str = "diff --git a/ffn/core.py b/ffn/core.py\n", **overrides: object
) -> None:
    """The submission record `check_handoff` reads the touched-tests stage from."""
    from tests.test_submission import passing_touched_tests

    submission = directory / "submission"
    submission.mkdir(exist_ok=True)
    (submission / "submission.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "diff_sha256": diff_sha256(diff),
                "ready": True,
                "touched_tests": passing_touched_tests(diff, **overrides),
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

    def test_an_unticked_template_box_claims_nothing(self) -> None:
        # Mailman #223: "- [ ] I have run make style" left unticked blocked handoff.
        body = "- [ ] I have run `make style` and fixed any issues\n* [ ] I have tested it\n"
        self.assertEqual(first_person_claims(body), [])

    def test_a_ticked_template_box_is_still_a_claim(self) -> None:
        claims = first_person_claims("- [x] I have run `make style` and fixed any issues\n")
        self.assertEqual([claim["line"] for claim in claims], [1])

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

    def test_a_dollar_in_the_title_is_not_expanded_by_powershell(self) -> None:
        # Inside "..." PowerShell expands $env:X and runs $(...); '...' does neither.
        command = publish_command(
            kind="pull-request",
            body_path=Path("/tmp/body.md"),
            repository="pmorissette/ffn",
            title="Fix $(Get-Date) and the user's $env:PATH",
            head="Mailman-Fork:mailman/run-1",
            base="master",
        )
        self.assertIn(
            "--title 'Fix $(Get-Date) and the user''s $env:PATH' --body-file", command
        )

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

    def test_a_hand_edited_affirmed_claim_does_not_crash_the_check(self) -> None:
        # `hunt status` calls the check; one record without "text" took it
        # down with a KeyError. Mailman #347.
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = _run_directory(root)
            body_path = root / "body.md"
            body_path.write_text(BODY, encoding="utf-8", newline="\n")
            build_handoff(
                run_id=run.run_id,
                run_directory=directory,
                body_path=body_path,
                kind="pull-request",
                repository="pmorissette/ffn",
                title="t",
                head="Mailman-Fork:mailman/run-1",
                base="master",
                owner_type_lookup=lambda _owner: None,
            )
            path = directory / HANDOFF_FILENAME
            record = json.loads(path.read_text(encoding="utf-8"))
            record["affirmed_claims"] = [{"line": 3}, "I tested this."]
            path.write_text(json.dumps(record), encoding="utf-8")
            result = check_handoff(directory)
            self.assertIn("reason", result)

    def test_an_issue_comment_does_not_replace_the_pull_request_record(self) -> None:
        # The comment overwrote handoff.json, and the PR's own verify command
        # then checked the comment and skipped every pull request check.
        # Mailman #347.
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = _run_directory(root)
            body_path = root / "body.md"
            body_path.write_text(BODY, encoding="utf-8", newline="\n")
            build_handoff(
                run_id=run.run_id,
                run_directory=directory,
                body_path=body_path,
                kind="pull-request",
                repository="pmorissette/ffn",
                title="t",
                head="Mailman-Fork:mailman/run-1",
                base="master",
                owner_type_lookup=lambda _owner: None,
            )
            reply = root / "reply.md"
            reply.write_text("Thanks for the report.\n", encoding="utf-8")
            comment, block = build_handoff(
                run_id=run.run_id,
                run_directory=directory,
                body_path=reply,
                kind="issue-comment",
                repository="pmorissette/ffn",
                issue_number=327,
                pull_request_lookup=lambda _repository, _number: None,
            )
            self.assertEqual(load_handoff(directory)["kind"], "pull-request")
            self.assertIn("--comment", comment["verify_command"])
            self.assertIn(comment["verify_command"], block)
            self.assertTrue(check_handoff(directory, comment=True)["ok"])

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

    def _prepared(
        self, root: Path, body: str, affirmed: list[int] | None = None
    ) -> tuple[Path, Path]:
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
            affirmed_lines=affirmed or [],
        )
        _prior_art(directory)
        return directory, body_path

    def test_an_unchanged_body_passes(self) -> None:
        with TemporaryDirectory() as name:
            directory, _ = self._prepared(Path(name), BODY)
            self.assertTrue(check_handoff(directory)["ok"])

    def test_an_unaffirmed_first_person_line_blocks(self) -> None:
        with TemporaryDirectory() as name:
            directory, _ = self._prepared(Path(name), CLAIMING_BODY)
            self.assertEqual(check_handoff(directory)["reason"], "first-person-claims")

    def test_a_line_the_operator_affirmed_passes(self) -> None:
        # #217: biopython's PR template requires "I have read the
        # CONTRIBUTING.rst file, have run pre-commit"; the operator did both
        # and approved the box in chat.
        line = next(claim["line"] for claim in first_person_claims(CLAIMING_BODY))
        with TemporaryDirectory() as name:
            directory, _ = self._prepared(Path(name), CLAIMING_BODY, affirmed=[line])
            result = check_handoff(directory)
            self.assertTrue(result["ok"], result)
            record = json.loads((directory / HANDOFF_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(record["first_person_claims"], [])
            self.assertEqual(
                [claim["line"] for claim in record["affirmed_claims"]], [line]
            )

    def test_affirming_a_line_that_is_not_a_claim_is_refused(self) -> None:
        with TemporaryDirectory() as name, self.assertRaises(ValueError):
            self._prepared(Path(name), CLAIMING_BODY, affirmed=[1])

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


class TouchedTestsGateTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/115

    edgartools#1329 failed CI on a test file that imports the changed module
    and that nobody ran before filing. The filing check now refuses a run
    whose touched tests failed or never ran for the diff being pushed.
    """

    def setUp(self):
        authors = patch("mailman.handoff.check_authorship", return_value={"ok": True, "head": "fixture"})
        authors.start()
        self.addCleanup(authors.stop)
        foreign = patch("mailman.handoff.foreign_pull_request", return_value=None)
        foreign.start()
        self.addCleanup(foreign.stop)

    def _prepared(self, root: Path) -> Path:
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
        _prior_art(directory)
        return directory

    def test_a_passing_stage_is_reported_with_the_check(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name))
            result = check_handoff(directory)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["touched_tests"]["reason"], "touched-tests-passed")
            self.assertEqual(result["touched_tests"]["selected"], ["tests/test_thing.py"])
            self.assertEqual(result["touched_tests"]["passed"], 3)

    def test_a_failed_stage_refuses(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name))
            _touched_tests(directory, exit_code=1, passed=2, failed=1)
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "touched-tests-failed")
            self.assertIn("failed 1", result["detail"])

    def test_a_stage_that_never_ran_refuses(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name))
            _touched_tests(directory, ran=False, reason="no-environment-python", exit_code=None)
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "touched-tests-not-run")
            self.assertIn("no-environment-python", result["detail"])

    def test_a_missing_submission_refuses(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name))
            (directory / "submission" / "submission.json").unlink()
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "touched-tests-not-run")

    def test_a_record_for_an_earlier_export_refuses(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name))
            export = directory / "export"
            export.mkdir()
            (export / "changes.diff").write_text(
                "diff --git a/ffn/core.py b/ffn/core.py\n+new line\n", encoding="utf-8"
            )
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "touched-tests-not-run")
            self.assertIn("export changed", result["detail"])


class OwnWordsHandoffTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/181

    A target that wants the description in the author's own words gets an
    agent-written body from Mailman. The run may be packaged and reported
    ready, but nothing may publish that body: the handoff prints no command
    and handoff-check refuses until the human has rewritten it.
    """

    def setUp(self):
        authors = patch("mailman.handoff.check_authorship", return_value={"ok": True, "head": "fixture"})
        authors.start()
        self.addCleanup(authors.stop)
        foreign = patch("mailman.handoff.foreign_pull_request", return_value=None)
        foreign.start()
        self.addCleanup(foreign.stop)

    def _hold(self, directory: Path, codes: list[str]) -> None:
        path = directory / "submission" / "submission.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(ready=not codes, blocking_codes=codes)
        path.write_text(json.dumps(record), encoding="utf-8")

    def _handoff(self, directory: Path, body_path: Path) -> tuple[dict, str]:
        return build_handoff(
            run_id=directory.name,
            run_directory=directory,
            body_path=body_path,
            kind="pull-request",
            repository="pmorissette/ffn",
            title="Cache the rolling window",
            head="Mailman-Fork:mailman/run-1",
            base="master",
        )

    def _prepared(self, root: Path) -> tuple[Path, Path, dict, str]:
        _, directory = _run_directory(root)
        _prior_art(directory)
        self._hold(directory, ["policy-requires-own-words"])
        body_path = root / "body.md"
        body_path.write_text(BODY, encoding="utf-8", newline="\n")
        record, block = self._handoff(directory, body_path)
        return directory, body_path, record, block

    def test_an_agent_written_body_gets_no_publish_command(self) -> None:
        with TemporaryDirectory() as name:
            _, _, record, block = self._prepared(Path(name))

        self.assertIsNone(record["command"])
        self.assertTrue(record["own_words_pending"])
        self.assertNotIn("gh pr create", block)
        self.assertIn("OWN WORDS -- you must rewrite this body before filing", block)
        self.assertIn("COMMAND -- withheld", block)
        self.assertIn("own_words_confirmed", block)
        # The rewrite comes before the body it applies to.
        self.assertLess(block.index("OWN WORDS"), block.index("BODY"))

    def test_handoff_check_refuses_until_the_rewrite(self) -> None:
        with TemporaryDirectory() as name:
            directory, _, _, _ = self._prepared(Path(name))
            result = check_handoff(directory)

        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "own-words-pending")
        self.assertIn("own words", result["detail"])

    def test_any_other_refusal_is_reported_first(self) -> None:
        with TemporaryDirectory() as name:
            directory, body_path, _, _ = self._prepared(Path(name))
            body_path.write_text(BODY + "\nEdited.\n", encoding="utf-8")
            edited = check_handoff(directory)
            body_path.write_text(BODY, encoding="utf-8", newline="\n")
            _prior_art(directory, search_age_minutes=26 * 60)
            self._hold(directory, ["policy-requires-own-words"])
            stale = check_handoff(directory)

        self.assertEqual(edited["reason"], "body-changed")
        self.assertEqual(stale["reason"], "duplicate-search-stale")

    def test_a_handoff_printed_before_the_confirmation_still_refuses(self) -> None:
        with TemporaryDirectory() as name:
            directory, _, _, _ = self._prepared(Path(name))
            self._hold(directory, [])
            result = check_handoff(directory)

        self.assertEqual(result["reason"], "own-words-pending")

    def test_the_rewritten_and_confirmed_body_gets_its_command(self) -> None:
        with TemporaryDirectory() as name:
            directory, body_path, _, _ = self._prepared(Path(name))
            body_path.write_text("Rewritten by the author.\n", encoding="utf-8")
            self._hold(directory, [])
            record, block = self._handoff(directory, body_path)
            result = check_handoff(directory)

        self.assertFalse(record["own_words_pending"])
        self.assertIn("gh pr create", block)
        self.assertNotIn("OWN WORDS", block)
        self.assertTrue(result["ok"], result)

    def test_own_words_beside_another_code_still_withholds_the_command(self) -> None:
        with TemporaryDirectory() as name:
            _, directory = _run_directory(Path(name))
            _prior_art(directory)
            self._hold(directory, ["lint-failed", "policy-requires-own-words"])
            body_path = Path(name) / "body.md"
            body_path.write_text(BODY, encoding="utf-8", newline="\n")
            record, _ = self._handoff(directory, body_path)

        self.assertIsNone(record["command"])


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
        foreign = patch("mailman.handoff.foreign_pull_request", return_value=None)
        foreign.start()
        self.addCleanup(foreign.stop)

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
        else:
            _touched_tests(directory)
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
            # The hint is the command, query included: `duplicate-search`
            # refuses to run without --query. Pylint run
            # 20260930T090344Z-25c317 spent a round trip finding it.
            self.assertIn(
                f"mailman duplicate-search {directory.name} "
                "--query 'rolling window cache'",
                result["detail"],
            )

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

    # Mailman #348: the refreshed search was read for its age alone, so one
    # that half failed, or that found an open pull request for this issue,
    # re-armed the run for filing.
    RIVAL = {
        "number": 400,
        "pull_request": True,
        "state": "open",
        "references_issue": True,
        "title": "Cache the rolling window",
    }

    def test_an_incomplete_search_clears_nothing(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name), complete=False)
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "duplicate-search-incomplete")

    def test_a_strong_match_the_submission_never_weighed_refuses(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name), matches=[self.RIVAL])
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "duplicate-search-new-match")
            self.assertIn("pr#400", result["detail"])
            self.assertIn("prepare-submission", result["detail"])

    def test_the_runs_own_filed_pull_request_is_no_rival(self) -> None:
        # A filed run's refreshed search finds its own pull request (#97).
        from mailman.provenance import provenance_path

        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name), matches=[self.RIVAL])
            provenance_path(directory).write_text(
                json.dumps({"pull_request": 400}), encoding="utf-8"
            )
            result = check_handoff(directory)
            self.assertTrue(result["ok"], result)

    def test_a_strong_match_the_submission_weighed_passes(self) -> None:
        with TemporaryDirectory() as name:
            directory = self._prepared(Path(name), matches=[self.RIVAL])
            path = directory / "submission" / "submission.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            record["duplicate_candidates"] = {"strong": ["pr#400"]}
            path.write_text(json.dumps(record), encoding="utf-8")
            result = check_handoff(directory)
            self.assertTrue(result["ok"], result)

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
            self.assertTrue(check_handoff(directory, comment=True)["ok"])

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


class SpecificationCitationTests(unittest.TestCase):
    """A body that leans on a specification quotes the clause it leans on.

    py-pdf/pypdf#4105 said "the specification says" with no clause and no
    quote; the maintainer asked whether the specification had been read, and
    the sentence (ISO 32000-1, 9.10.3) was found by hand afterwards.
    See https://github.com/wolfgang-aura/Mailman/issues/124.
    """

    def test_an_unsourced_appeal_to_the_specification_is_found(self) -> None:
        found = unsourced_specification_claims(
            "Fix ToUnicode parsing.\n\nThe specification says a two-byte "
            "code is read as one character.\n"
        )
        self.assertEqual(len(found), 1)
        self.assertIn("specification says", found[0]["text"])

    def test_each_named_standard_needs_the_same_support(self) -> None:
        for text in (
            "ISO 32000 requires this.",
            "Per the spec, the field is optional.",
            "RFC 3986 allows an empty authority.",
        ):
            with self.subTest(text=text):
                self.assertEqual(len(unsourced_specification_claims(text)), 1)

    def test_a_short_spec_counts_only_where_it_is_cited(self) -> None:
        # spack calls its build object a spec; the run's body said "applies to
        # the spec" and handoff-check refused it. Mailman #295.
        for text, expected in (
            ("For every resource that applies to the spec (`x()`), it adds one.", 0),
            ("The DAG hash of the spec is unchanged.", 0),
            ("According to the spec, the field is optional.", 1),
            ("The spec requires an empty authority.", 1),
        ):
            with self.subTest(text=text):
                self.assertEqual(len(unsourced_specification_claims(text)), expected)

    def test_a_clause_and_a_quoted_sentence_satisfy_it(self) -> None:
        self.assertEqual(
            unsourced_specification_claims(
                'ISO 32000-1, 9.10.3: "If the font is a simple font, the '
                'code is a single byte."'
            ),
            [],
        )

    def test_a_block_quote_after_the_citation_counts_as_the_quote(self) -> None:
        self.assertEqual(
            unsourced_specification_claims(
                "RFC 3986 section 3.2 says:\n\n> The authority component is "
                "preceded by a double slash.\n"
            ),
            [],
        )

    def test_a_clause_without_a_quote_is_still_unsourced(self) -> None:
        self.assertEqual(
            len(unsourced_specification_claims("See the specification, 9.10.3.")),
            1,
        )

    @patch("mailman.handoff.foreign_pull_request", return_value=None)
    @patch("mailman.handoff.check_authorship", return_value={"ok": True, "head": "x"})
    def test_handoff_check_refuses_the_body(self, *_mocks) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = _run_directory(root)
            body_path = root / "body.md"
            body_path.write_text(
                BODY + "\nThe specification says this is the right reading.\n",
                encoding="utf-8",
                newline="\n",
            )
            build_handoff(
                run_id=run.run_id,
                run_directory=directory,
                body_path=body_path,
                kind="issue-comment",
                repository="pmorissette/ffn",
                issue_number=327,
            )
            result = check_handoff(directory, comment=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "unsourced-specification-claims")


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
        # #21967 is somebody else's pull request; everything else is an issue.
        foreign = patch(
            "mailman.handoff.foreign_pull_request",
            side_effect=lambda _repo, number: "someone" if number == 21967 else None,
        )
        foreign.start()
        self.addCleanup(foreign.stop)

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
            record, _ = self._comment(root, directory, 21960)
            self.assertFalse(record["closure"]["closed"])
            with self.assertRaises(ValueError):
                self._comment(root, directory, 21960, closing_reply=True)

    def test_somebody_elses_pull_request_is_refused_while_ours_is_open(self) -> None:
        # The comment that drew the complaint went out twelve seconds before
        # our pull request closed. The close is not what makes it wrong.
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            with self.assertRaises(ValueError) as caught:
                self._comment(root, directory, 21967)
            self.assertIn("pull request by someone, not ours", str(caught.exception))
            # And the override does not reach it until provenance names it.
            with self.assertRaises(ValueError):
                self._comment(root, directory, 21967, closing_reply=True)

    def test_a_long_reply_is_flagged_as_a_review(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            body_path = root / "reply.md"
            body_path.write_text(("word " * 158).strip(), encoding="utf-8")
            record, block = build_handoff(
                run_id=directory.name,
                run_directory=directory,
                body_path=body_path,
                kind="issue-comment",
                repository="python/mypy",
                issue_number=21960,
            )
            self.assertEqual(record["word_count"], 158)
            self.assertIn("LENGTH -- 158 words", block)

    def test_one_closing_reply_is_allowed_and_a_second_thread_is_not(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            _close_the_case(directory)
            record, block = self._comment(root, directory, 21967, closing_reply=True)
            self.assertTrue(record["closing_reply"])
            self.assertIn("CLOSING REPLY", block)
            self.assertTrue(check_handoff(directory, comment=True)["ok"])
            # Re-rendering the same reply is fine; the marker names the thread.
            self._comment(root, directory, 21967, closing_reply=True)
            with self.assertRaises(ValueError) as caught:
                self._comment(root, directory, 21960, closing_reply=True)
            self.assertIn("already went to #21967", str(caught.exception))

    def test_a_second_reply_to_the_same_thread_is_refused(self) -> None:
        # The marker named the thread, so any text to that thread passed:
        # one closing reply could become several. Mailman #347.
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            _close_the_case(directory)
            self._comment(root, directory, 21967, closing_reply=True)
            second = root / "second.md"
            second.write_text("One more thing about the cache.\n", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                build_handoff(
                    run_id=directory.name,
                    run_directory=directory,
                    body_path=second,
                    kind="issue-comment",
                    repository="python/mypy",
                    issue_number=21967,
                    closing_reply=True,
                )
            self.assertIn("already went to #21967", str(caught.exception))

    def test_an_unreadable_closing_reply_marker_is_refused(self) -> None:
        # A malformed marker read as "no reply yet". Mailman #347.
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            _close_the_case(directory)
            (directory / "closing-reply.json").write_text("{\"issue", encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                self._comment(root, directory, 21967, closing_reply=True)
            self.assertIn("closing-reply.json", str(caught.exception))

    def test_the_case_closing_after_the_handoff_stops_the_publish(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            record, _ = self._comment(root, directory, 21960)
            self.assertTrue(check_handoff(directory, comment=True)["ok"])
            _close_the_case(directory)
            result = check_handoff(directory, comment=True)
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


class SilentCloseTests(unittest.TestCase):
    """pdm#3884 and pytest#14993 both closed without a word. The first was
    filed on an issue its maintainer had already closed; the second on an
    issue nobody from the project had answered.
    See https://github.com/wolfgang-aura/Mailman/issues/88."""

    def setUp(self):
        authors = patch(
            "mailman.handoff.check_authorship",
            return_value={"ok": True, "head": "fixture"},
        )
        authors.start()
        self.addCleanup(authors.stop)
        foreign = patch("mailman.handoff.foreign_pull_request", return_value=None)
        foreign.start()
        self.addCleanup(foreign.stop)

    def _claims(self, directory: Path, **fields) -> None:
        _prior_art(directory)
        path = directory / "claims.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record.update(fields)
        path.write_text(json.dumps(record), encoding="utf-8", newline="\n")

    def _pull_request(self, root: Path, directory: Path):
        body_path = root / "body.md"
        body_path.write_text(BODY, encoding="utf-8", newline="\n")
        return build_handoff(
            run_id=directory.name,
            run_directory=directory,
            body_path=body_path,
            kind="pull-request",
            repository="pmorissette/ffn",
            title="t",
            head="wolfgang-aura:b",
            base="master",
        )

    def test_a_closed_issue_refuses_the_publish(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            self._pull_request(root, directory)
            self._claims(
                directory, issue_state="closed", issue_closed_at="2026-09-08T09:27:46Z"
            )
            result = check_handoff(directory)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "issue-closed")
            self.assertIn("2026-09-08T09:27:46Z", result["detail"])

    def test_an_open_issue_still_publishes(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            self._pull_request(root, directory)
            self._claims(directory, issue_state="open")
            self.assertTrue(check_handoff(directory)["ok"])

    def test_an_outside_report_nobody_answered_is_flagged(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            self._claims(
                directory, reporter_association="NONE", maintainer_replied=False
            )
            record, block = self._pull_request(root, directory)
            self.assertIn("UNTRIAGED ISSUE", block)
            self.assertIn("(NONE)", record["triage_warning"])

    def test_a_maintainer_reply_or_report_clears_the_flag(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            self._claims(
                directory, reporter_association="NONE", maintainer_replied=True
            )
            record, _ = self._pull_request(root, directory)
            self.assertIsNone(record["triage_warning"])
            self._claims(
                directory, reporter_association="MEMBER", maintainer_replied=False
            )
            record, _ = self._pull_request(root, directory)
            self.assertIsNone(record["triage_warning"])

    def test_an_older_claims_record_gets_no_flag(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            _, directory = _run_directory(root)
            self._claims(directory)
            record, _ = self._pull_request(root, directory)
            self.assertIsNone(record["triage_warning"])


OFFER = (
    "@maintainer This reproduces on master at 01234567; the reproduction is "
    "the two-line script in the issue. A fix is ready. Would you like a PR?\n"
)


class OfferHandoffTests(unittest.TestCase):
    """An ask-first run carries its offer comment and its PR handoff at once.

    `handoff --kind issue-comment` wrote the same `handoff.json` as the pull
    request, so the offer and the PR replaced each other.
    https://github.com/wolfgang-aura/Mailman/issues/138
    """

    def setUp(self):
        authors = patch("mailman.handoff.check_authorship", return_value={"ok": True, "head": "fixture"})
        authors.start()
        self.addCleanup(authors.stop)

    def _run(self, root: Path) -> tuple[RunRecord, Path]:
        run, directory = _run_directory(root)
        record = json.loads((directory / "run.json").read_text(encoding="utf-8"))
        record["issue"] = "https://github.com/pmorissette/ffn/issues/327"
        (directory / "run.json").write_text(json.dumps(record), encoding="utf-8")
        (directory / "offer-comment.md").write_text(OFFER, encoding="utf-8", newline="\n")
        _prior_art(directory)
        return run, directory

    def _offer(self, run: RunRecord, directory: Path, *, issue: int = 327, body: Path | None = None):
        return build_handoff(
            run_id=run.run_id,
            run_directory=directory,
            body_path=body or directory / "offer-comment.md",
            kind="issue-comment",
            repository="pmorissette/ffn",
            issue_number=issue,
            offer=True,
            pull_request_lookup=lambda *_: None,
        )

    def test_the_offer_and_the_pull_request_handoffs_coexist(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = self._run(root)
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
            _, block = self._offer(run, directory)

            self.assertTrue((directory / HANDOFF_FILENAME).is_file())
            self.assertTrue((directory / OFFER_HANDOFF_FILENAME).is_file())
            self.assertEqual(load_handoff(directory)["kind"], "pull-request")
            self.assertEqual(load_offer_handoff(directory)["kind"], "issue-comment")
            self.assertIn("gh issue comment 327 --repo pmorissette/ffn", block)
            self.assertTrue(check_handoff(directory)["ok"])
            offer = check_handoff(directory, offer=True)
            self.assertTrue(offer["ok"], offer)
            self.assertEqual(offer["issue_number"], 327)

    def test_an_offer_to_another_thread_is_refused(self) -> None:
        with TemporaryDirectory() as name:
            run, directory = self._run(Path(name))
            with self.assertRaisesRegex(ValueError, "this run's issue is #327"):
                self._offer(run, directory, issue=999)
            self.assertIsNone(load_offer_handoff(directory))

    def test_an_offer_the_decision_would_refuse_is_refused(self) -> None:
        with TemporaryDirectory() as name:
            run, directory = self._run(Path(name))
            long = directory / "long.md"
            long.write_text(OFFER + "word " * 120, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "keep an offer under 120"):
                self._offer(run, directory, body=long)
            bare = directory / "bare.md"
            bare.write_text("It reproduces. Would you like a PR for this?\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not name the base commit"):
                self._offer(run, directory, body=bare)
            self.assertIsNone(load_offer_handoff(directory))

    def test_offer_check_fails_without_a_record_or_on_a_tampered_one(self) -> None:
        with TemporaryDirectory() as name:
            run, directory = self._run(Path(name))
            missing = check_handoff(directory, offer=True)
            self.assertFalse(missing["ok"])
            self.assertEqual(missing["reason"], "no-offer-handoff")

            self._offer(run, directory)
            path = directory / OFFER_HANDOFF_FILENAME
            record = json.loads(path.read_text(encoding="utf-8"))
            record["issue_number"] = 12
            path.write_text(json.dumps(record), encoding="utf-8")
            tampered = check_handoff(directory, offer=True)
            self.assertFalse(tampered["ok"])
            self.assertEqual(tampered["reason"], "offer-invalid")

    def test_offer_check_fails_when_the_draft_changes(self) -> None:
        with TemporaryDirectory() as name:
            run, directory = self._run(Path(name))
            self._offer(run, directory)
            (directory / "offer-comment.md").write_text(OFFER + "Thanks!\n", encoding="utf-8")
            changed = check_handoff(directory, offer=True)
            self.assertFalse(changed["ok"])
            self.assertEqual(changed["reason"], "body-changed")

    def test_the_cli_writes_and_checks_the_offer_record(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            run, directory = self._run(root)
            stream = io.StringIO()
            with redirect_stdout(stream):
                made = main([
                    "handoff", run.run_id, "--offer", "--kind", "issue-comment",
                    "--issue", "327", "--repo", "pmorissette/ffn",
                    "--body", str(directory / "offer-comment.md"),
                    "--data-root", str(root),
                ])
                checked = main(["handoff-check", run.run_id, "--offer", "--data-root", str(root)])
            self.assertEqual((made, checked), (0, 0), stream.getvalue())
            self.assertFalse((directory / HANDOFF_FILENAME).exists())
            self.assertTrue((directory / OFFER_HANDOFF_FILENAME).is_file())


class BodyCheckCommandTests(unittest.TestCase):
    """The body lints ran only inside package, which the agent cannot run, so
    DOT's "RFC 7592 PUT" reached the operator before anyone saw it. #413."""

    def check(self, text: str, *affirm: str) -> tuple[int, dict]:
        with TemporaryDirectory() as directory:
            body = Path(directory) / "body.md"
            body.write_text(text, encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["body-check", str(body), *affirm])
        return code, json.loads(output.getvalue())

    def test_an_unsourced_standard_fails_before_package(self) -> None:
        code, result = self.check("Fix it.\n\nDCR POST and RFC 7592 PUT reject the URI.\n")
        self.assertEqual(code, 1)
        self.assertEqual(result["codes"], ["unsourced-specification-claims"])

    def test_first_person_claims_fail_unless_affirmed(self) -> None:
        code, result = self.check(CLAIMING_BODY)
        self.assertEqual((code, result["codes"]), (1, ["first-person-claims"]))
        line = result["first_person_claims"][0]["line"]
        code, result = self.check(CLAIMING_BODY, "--affirm", str(line))
        self.assertEqual((code, result["codes"]), (0, []))
