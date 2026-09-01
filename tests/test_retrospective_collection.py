from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from mailman.artifacts import (
    append_agent_execution,
    append_verification,
    create_run,
    load_run,
    write_run,
)
from mailman.cli import main
from mailman.knowledge.collect import collect_retrospective, write_retrospective
from mailman.knowledge.retrospective import UNRECORDED, Retrospective
from mailman.knowledge.taxonomy import EvidenceChannel, Outcome, PatternCategory
from mailman.models import RunStatus


def make_run(root: Path):
    return create_run(
        repository="https://github.com/example/project.git",
        issue="https://github.com/example/project/issues/9",
        base_commit="a" * 40,
        primary="codex",
        reviewer="claude",
        primary_model="codex-test-model",
        reviewer_model="claude-test-model",
        data_root=root,
    )


def execution(role: str, *, exit_code: int, report_present: bool, timed_out=False):
    return {
        "agent": role,
        "role": role,
        "report_present": report_present,
        "process": {"exit_code": exit_code, "timed_out": timed_out},
    }


def write_orchestration(run_directory: Path, steps: list[dict], **extra) -> None:
    record = {"schema_version": 1, "steps": steps}
    record.update(extra)
    (run_directory / "orchestration.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8"
    )


class CollectionTests(unittest.TestCase):
    def test_versions_come_from_the_run_and_its_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            prompts = run_directory / "prompts"
            prompts.mkdir()
            (prompts / "0001-primary.md").write_text("primary", encoding="utf-8")
            (prompts / "0002-reviewer.md").write_text("reviewer", encoding="utf-8")
            skill = Path(temporary_directory) / "SKILL.md"
            skill.write_text(
                "---\nname: open-source-engineering\nversion: v0.3\n---\n",
                encoding="utf-8",
            )

            retrospective = collect_retrospective(
                run, run_directory, skill_path=skill
            )
            versions = retrospective.versions

            self.assertEqual(versions.skill_version, "v0.3")
            self.assertTrue(versions.primary_prompt_version.startswith("sha256:"))
            self.assertNotEqual(
                versions.primary_prompt_version, versions.review_prompt_version
            )
            self.assertEqual(versions.primary_model, "codex-test-model")
            self.assertEqual(versions.reviewer_model, "claude-test-model")

    def test_an_unversioned_skill_is_recorded_as_such(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            skill = Path(temporary_directory) / "SKILL.md"
            skill.write_text("---\nname: x\n---\n", encoding="utf-8")

            retrospective = collect_retrospective(
                run, run_directory, skill_path=skill
            )

            self.assertEqual(retrospective.versions.skill_version, "unversioned")
            self.assertEqual(
                retrospective.versions.primary_prompt_version, UNRECORDED
            )

    def test_failed_verification_and_agent_records_become_observations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            append_verification(run_directory, {"exit_code": 0, "timed_out": False})
            append_verification(run_directory, {"exit_code": 1, "timed_out": False})
            append_agent_execution(
                run_directory,
                "primary",
                execution("primary", exit_code=0, report_present=False),
            )

            retrospective = collect_retrospective(run, run_directory)
            summaries = [item.summary for item in retrospective.observations]

            self.assertEqual(len(retrospective.observations), 2)
            self.assertIn("Verification record 2 exited with code 1.", summaries)
            self.assertIn(
                "The primary agent exited zero without writing a report.", summaries
            )
            no_report = retrospective.observations[1]
            self.assertEqual(no_report.category, PatternCategory.TOOL_USAGE)
            self.assertEqual(
                no_report.channel, EvidenceChannel.PRIMARY_AGENT_FAILURE
            )
            self.assertIn("exits zero", no_report.assumption or "")
            self.assertEqual(retrospective.run_facts["failed_verification_records"], [2])

    def test_a_reviewer_timeout_is_a_harness_observation_not_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            append_agent_execution(
                run_directory,
                "reviewer",
                execution(
                    "reviewer", exit_code=None, report_present=False, timed_out=True
                ),
            )

            observation = collect_retrospective(run, run_directory).observations[0]

            self.assertEqual(
                observation.channel, EvidenceChannel.AUTOMATED_VERIFICATION
            )
            self.assertEqual(observation.category, PatternCategory.ENVIRONMENT)

    def test_orchestration_steps_replace_the_raw_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            append_verification(run_directory, {"exit_code": 1, "timed_out": False})
            write_orchestration(
                run_directory,
                [
                    {"name": "workspace", "ok": True, "detail": "clean", "data": {}},
                    {
                        "name": "verification:primary",
                        "ok": False,
                        "detail": "verification exited with code 1",
                        "data": {"record": 1, "exit_code": 1},
                    },
                    {
                        "name": "blocked",
                        "ok": False,
                        "detail": "independent verification failed",
                        "data": {},
                    },
                ],
                revisions_used=0,
            )

            retrospective = collect_retrospective(run, run_directory)
            categories = [item.category for item in retrospective.observations]

            # Two observations, not three: the raw verification record is not
            # replayed on top of the step that already explains it.
            self.assertEqual(len(retrospective.observations), 2)
            self.assertEqual(
                categories,
                [PatternCategory.VERIFICATION, PatternCategory.VERIFICATION],
            )
            self.assertIn(
                "Mailman blocked the run", retrospective.observations[1].summary
            )
            self.assertEqual(retrospective.run_facts["failed_steps"], [
                "verification:primary",
                "blocked",
            ])

    def test_a_revise_verdict_is_recorded_as_a_reviewer_finding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            write_orchestration(
                run_directory,
                [
                    {
                        "name": "verdict",
                        "ok": True,
                        "detail": "reviewer verdict REVISE",
                        "data": {"verdict": "REVISE"},
                    }
                ],
                revisions_used=1,
            )

            observations = collect_retrospective(run, run_directory).observations

            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].channel, EvidenceChannel.REVIEWER_FINDING)
            self.assertEqual(observations[0].category, PatternCategory.REVIEW)

    def test_a_clean_run_seeds_one_success_and_no_opinion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            run.transition(RunStatus.PRIMARY_RUNNING, "start")
            run.transition(RunStatus.REVIEW_PENDING, "review")
            run.transition(RunStatus.VERIFICATION_PENDING, "approved")
            run.transition(RunStatus.READY_FOR_HUMAN_REVIEW, "verified")
            write_run(run, run_directory)
            write_orchestration(run_directory, [], revisions_used=0)

            observations = collect_retrospective(run, run_directory).observations

            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].outcome, Outcome.SUCCESS)
            self.assertIn("no revision", observations[0].summary)
            self.assertIn("not the same as being right", observations[0].detail)

    def test_a_blocked_run_without_steps_still_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            run.transition(RunStatus.BLOCKED, "agent executable missing")
            write_run(run, run_directory)

            observations = collect_retrospective(run, run_directory).observations

            self.assertEqual(len(observations), 1)
            self.assertEqual(observations[0].outcome, Outcome.FAILURE)
            self.assertIn("BLOCKED", observations[0].summary)


class WriteTests(unittest.TestCase):
    def test_both_files_are_written_and_the_record_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            retrospective = collect_retrospective(run, run_directory)
            json_path, markdown_path = write_retrospective(
                retrospective, run_directory
            )

            restored = Retrospective.from_dict(
                json.loads(json_path.read_text(encoding="utf-8"))
            )
            self.assertEqual(restored.to_dict(), retrospective.to_dict())
            self.assertIn(
                "# Retrospective for run", markdown_path.read_text(encoding="utf-8")
            )
            self.assertFalse(json_path.with_suffix(".json.tmp").exists())

    def test_an_existing_retrospective_is_not_silently_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            retrospective = collect_retrospective(run, run_directory)
            write_retrospective(retrospective, run_directory)
            (run_directory / "retrospective.md").write_text(
                "answers a person wrote", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "--force"):
                write_retrospective(retrospective, run_directory)
            self.assertEqual(
                (run_directory / "retrospective.md").read_text(encoding="utf-8"),
                "answers a person wrote",
            )

            write_retrospective(retrospective, run_directory, force=True)
            self.assertIn(
                "# Retrospective for run",
                (run_directory / "retrospective.md").read_text(encoding="utf-8"),
            )


class RetrospectiveCommandTests(unittest.TestCase):
    def run_command(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_command_writes_a_template_and_reports_what_it_seeded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, run_directory = make_run(root)
            append_verification(run_directory, {"exit_code": 1, "timed_out": False})

            exit_code, stdout, stderr = self.run_command(
                ["retrospective", run.run_id, "--data-root", str(root)]
            )
            summary = json.loads(stdout[: stdout.rindex("}") + 1])

            self.assertEqual(exit_code, 0, stderr)
            self.assertEqual(summary["seeded_observations"], 1)
            self.assertEqual(summary["open_questions"], 12)
            self.assertFalse(summary["completed"])
            self.assertEqual(summary["channels"], {"AUTOMATED_VERIFICATION": 1})
            self.assertTrue((run_directory / "retrospective.json").is_file())
            self.assertTrue((run_directory / "retrospective.md").is_file())

    def test_command_refuses_to_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, _ = make_run(root)
            arguments = ["retrospective", run.run_id, "--data-root", str(root)]

            self.assertEqual(self.run_command(arguments)[0], 0)
            exit_code, _, stderr = self.run_command(arguments)

            self.assertEqual(exit_code, 2)
            self.assertIn("--force", stderr)
            self.assertEqual(self.run_command([*arguments, "--force"])[0], 0)

    def test_command_does_not_change_the_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "runs"
            run, _ = make_run(root)

            self.run_command(["retrospective", run.run_id, "--data-root", str(root)])

            reloaded, _ = load_run(run.run_id, root)
            self.assertEqual(reloaded.status, RunStatus.INITIALIZED)
            self.assertEqual(reloaded.history, [])


if __name__ == "__main__":
    unittest.main()
