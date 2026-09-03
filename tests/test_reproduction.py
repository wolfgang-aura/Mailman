from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from mailman.artifacts import create_run
from mailman.cli import main
from mailman.executor import CommandResult
from mailman.reproduction import (
    Expectation,
    evaluate,
    load_reproduction,
    record_command_reproduction,
    record_human_reproduction,
)
from mailman.targeting import (
    BUG_NOT_REPRODUCED,
    NO_REPRODUCTION,
    UNVERIFIED_REPRODUCTION,
    assess_target,
)


def _result(
    *,
    exit_code: int,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> CommandResult:
    return CommandResult(
        command=[sys.executable, "-m", "pytest"],
        working_directory=".",
        started_at="2026-09-03T00:00:00+00:00",
        duration_seconds=1.0,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
        timeout_seconds=900.0,
        environment={},
    )


class EvaluateTests(unittest.TestCase):
    def test_a_failing_command_is_the_bug_by_default(self) -> None:
        outcome = evaluate(_result(exit_code=1), Expectation())
        self.assertTrue(outcome.reproduced)

    def test_a_passing_command_means_there_is_nothing_to_fix(self) -> None:
        outcome = evaluate(_result(exit_code=0), Expectation())
        self.assertFalse(outcome.reproduced)
        self.assertEqual(
            [check.name for check in outcome.failures()], ["exit-code"]
        )

    def test_output_separates_two_trees_that_both_fail(self) -> None:
        """pytest #14964: fixed and unfixed both exit 1, the counts differ."""
        expectation = Expectation(required_output=("2 passed, 1 error",))
        unfixed = evaluate(
            _result(exit_code=1, stdout="== 2 passed, 1 error in 0.30s =="),
            expectation,
        )
        fixed = evaluate(
            _result(exit_code=1, stdout="== 1 passed, 2 errors in 0.30s =="),
            expectation,
        )
        self.assertTrue(unfixed.reproduced)
        self.assertFalse(fixed.reproduced)
        self.assertEqual(
            [check.name for check in fixed.failures()], ["required-output"]
        )

    def test_forbidden_output_can_state_the_fixed_shape(self) -> None:
        outcome = evaluate(
            _result(exit_code=1, stderr="DeprecationWarning: renamed"),
            Expectation(forbidden_output=("DeprecationWarning",)),
        )
        self.assertFalse(outcome.reproduced)

    def test_an_expected_exit_code_is_matched_exactly(self) -> None:
        expectation = Expectation(exit_code=2)
        self.assertTrue(evaluate(_result(exit_code=2), expectation).reproduced)
        self.assertFalse(evaluate(_result(exit_code=1), expectation).reproduced)

    def test_a_timeout_is_not_evidence_of_the_reported_bug(self) -> None:
        outcome = evaluate(_result(exit_code=1, timed_out=True), Expectation())
        self.assertFalse(outcome.reproduced)
        self.assertEqual([check.name for check in outcome.checks], ["completed"])


class RecordTests(unittest.TestCase):
    def test_a_command_record_keeps_the_checks_it_ran(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = record_command_reproduction(
                root,
                result=_result(exit_code=1, stdout="2 passed, 1 error"),
                expectation=Expectation(required_output=("2 passed, 1 error",)),
                working_directory=root / "workspace",
                command_record=3,
                base_commit="b" * 40,
            )
            self.assertTrue(record["reproduced"])
            self.assertTrue(record["machine_checked"])
            self.assertEqual(record["command_record"], 3)
            self.assertEqual(record["base_commit"], "b" * 40)
            self.assertEqual(load_reproduction(root), record)

    def test_a_human_record_states_that_no_machine_checked_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = record_human_reproduction(
                root, note="the report is a screenshot of a Windows dialog"
            )
            self.assertIsNone(record["reproduced"])
            self.assertFalse(record["machine_checked"])
            self.assertIn("Windows dialog", record["note"])

    def test_a_human_record_without_a_note_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                record_human_reproduction(Path(temporary), note="   ")

    def test_a_missing_record_reads_as_absent_not_as_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(load_reproduction(Path(temporary)))


class ReproductionGateTests(unittest.TestCase):
    def _searched(self, root: Path) -> Path:
        (root / "duplicate-search.json").write_text(
            json.dumps({"success": True, "matches": []}), encoding="utf-8"
        )
        (root / "prior-art.json").write_text(
            json.dumps({"success": True, "attempts": []}), encoding="utf-8"
        )
        (root / "target-intel.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "repository": "example/project",
                    "window_days": 14,
                    "freshness": {"human_outside_merges": 4},
                    "assessment": {"passes_freshness_bar": True},
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_a_missing_reproduction_blocks_and_names_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(self._searched(Path(temporary)))
        self.assertIn(NO_REPRODUCTION, assessment.blocking)
        self.assertFalse(assessment.may_start)
        self.assertIn("mailman reproduce", assessment.summary())

    def test_acknowledging_prior_attempts_does_not_clear_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                self._searched(Path(temporary)), acknowledged=True
            )
        self.assertIn(NO_REPRODUCTION, assessment.blocking)

    def test_a_bug_that_no_longer_happens_blocks_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._searched(Path(temporary))
            record_command_reproduction(
                root,
                result=_result(exit_code=0, stdout="1 passed"),
                expectation=Expectation(),
                working_directory=root,
                command_record=1,
            )
            assessment = assess_target(root)
        self.assertIn(BUG_NOT_REPRODUCED, assessment.blocking)
        summary = assessment.summary()
        self.assertIn("did NOT happen at the base commit", summary)
        self.assertIn("exit-code", summary)

    def test_a_reproduced_bug_lets_the_run_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._searched(Path(temporary))
            record_command_reproduction(
                root,
                result=_result(exit_code=1, stdout="1 failed"),
                expectation=Expectation(),
                working_directory=root,
                command_record=1,
            )
            assessment = assess_target(root)
        self.assertEqual(assessment.blocking, [])
        self.assertIn("still happens at the base commit", assessment.summary())

    def test_a_human_reading_warns_rather_than_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._searched(Path(temporary))
            record_human_reproduction(root, note="no runnable reproducer given")
            assessment = assess_target(root)
        self.assertEqual(assessment.blocking, [])
        self.assertIn(UNVERIFIED_REPRODUCTION, assessment.warnings)
        self.assertIn("no runnable reproducer given", assessment.summary())


class ReproduceCliTests(unittest.TestCase):
    def _run(self, data_root: Path):
        run, run_directory = create_run(
            repository="https://github.com/example/project.git",
            issue="https://github.com/example/project/issues/7",
            base_commit="a" * 40,
            primary="codex",
            reviewer="claude",
            data_root=data_root,
        )
        (run_directory / "workspace").mkdir()
        return run, run_directory

    def _reproduce(self, arguments: list[str]):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_a_reproducing_command_exits_zero_and_records_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "runs"
            run, run_directory = self._run(data_root)
            exit_code, stdout, _ = self._reproduce(
                [
                    "reproduce",
                    run.run_id,
                    "--data-root",
                    str(data_root),
                    "--timeout",
                    "60",
                    "--expect-output",
                    "2 passed, 1 error",
                    "--",
                    sys.executable,
                    "-c",
                    "import sys; print('2 passed, 1 error'); sys.exit(1)",
                ]
            )
            self.assertEqual(exit_code, 0)
            summary = json.loads(stdout)
            self.assertTrue(summary["reproduced"])
            self.assertEqual(summary["base_commit"], "a" * 40)
            record = load_reproduction(run_directory)
            self.assertEqual(record["exit_code"], 1)
            self.assertTrue(
                (
                    run_directory
                    / "commands"
                    / f"{record['command_record']:04d}.json"
                ).is_file()
            )

    def test_an_already_fixed_bug_fails_the_command_and_says_which_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "runs"
            run, run_directory = self._run(data_root)
            exit_code, stdout, stderr = self._reproduce(
                [
                    "reproduce",
                    run.run_id,
                    "--data-root",
                    str(data_root),
                    "--timeout",
                    "60",
                    "--expect-output",
                    "2 passed, 1 error",
                    "--",
                    sys.executable,
                    "-c",
                    "import sys; print('1 passed, 2 errors'); sys.exit(1)",
                ]
            )
            self.assertEqual(exit_code, 3)
            self.assertFalse(json.loads(stdout)["reproduced"])
            self.assertIn("did not happen at " + "a" * 40, stderr)
            self.assertIn("required-output", stderr)
            self.assertFalse(load_reproduction(run_directory)["reproduced"])

    def test_a_human_reading_needs_a_note_and_takes_no_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "runs"
            run, run_directory = self._run(data_root)
            exit_code, _, stderr = self._reproduce(
                [
                    "reproduce",
                    run.run_id,
                    "--data-root",
                    str(data_root),
                    "--not-machine-reproducible",
                ]
            )
            self.assertEqual(exit_code, 2)
            self.assertIn("needs a note", stderr)
            self.assertIsNone(load_reproduction(run_directory))

            exit_code, _, stderr = self._reproduce(
                [
                    "reproduce",
                    run.run_id,
                    "--data-root",
                    str(data_root),
                    "--not-machine-reproducible",
                    "--note",
                    "the report is a screenshot",
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                ]
            )
            self.assertEqual(exit_code, 2)
            self.assertIn("takes no command", stderr)

            exit_code, stdout, _ = self._reproduce(
                [
                    "reproduce",
                    run.run_id,
                    "--data-root",
                    str(data_root),
                    "--not-machine-reproducible",
                    "--note",
                    "the report is a screenshot",
                ]
            )
            self.assertEqual(exit_code, 0)
            summary = json.loads(stdout)
            self.assertIsNone(summary["reproduced"])
            self.assertFalse(summary["machine_checked"])

    def test_a_run_with_no_workspace_says_what_to_run_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary) / "runs"
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                data_root=data_root,
            )
            exit_code, _, stderr = self._reproduce(
                [
                    "reproduce",
                    run.run_id,
                    "--data-root",
                    str(data_root),
                    "--",
                    sys.executable,
                    "-c",
                    "pass",
                ]
            )
            self.assertEqual(exit_code, 2)
            self.assertIn("prepare-workspace", stderr)
            self.assertIsNone(load_reproduction(run_directory))


if __name__ == "__main__":
    unittest.main()
