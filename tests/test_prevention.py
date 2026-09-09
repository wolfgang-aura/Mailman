"""Regressions for the PRHunt delay investigation.

Each test names the issue it holds shut. The evidence behind all six is in
`.mailman/scratch/prhunt-investigation.md`.
"""
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from mailman import health, hunt
from mailman.limits import STEP_BYTE_LIMIT, offload, truncate_stream


class LeaseTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/63"""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "runs"
        self.root.mkdir(parents=True)
        self.record = hunt.create_hunt(
            self.root, 1, primary="codex", primary_model="a",
            reviewer="claude", reviewer_model="b", owner="first",
        )

    def test_a_new_hunt_is_owned_by_its_creator(self):
        self.assertEqual(self.record["lease"]["owner"], "first")
        self.assertTrue(hunt.lease_state(self.record)["held"])

    def test_a_second_coordinator_is_refused_without_a_takeover(self):
        with self.assertRaisesRegex(ValueError, "owned by first"):
            hunt.require_lease(self.record, "second")
        with self.assertRaisesRegex(ValueError, "owned by first"):
            hunt.acquire_lease(self.root, self.record, owner="second")

    def test_the_owner_may_keep_working(self):
        hunt.require_lease(self.record, "first")
        hunt.acquire_lease(self.root, self.record, owner="first")
        self.assertEqual(self.record["lease"]["takeovers"], [])

    def test_a_takeover_records_who_it_took_the_hunt_from(self):
        hunt.acquire_lease(self.root, self.record, owner="second",
                           takeover_reason="first task hit its usage limit")
        takeover = self.record["lease"]["takeovers"][-1]
        self.assertEqual(takeover["previous_owner"], "first")
        self.assertTrue(takeover["was_live"])
        self.assertIn("usage limit", takeover["reason"])
        hunt.require_lease(self.record, "second")

    def test_an_expired_lease_is_adopted_deliberately_or_not_at_all(self):
        """https://github.com/wolfgang-aura/Mailman/issues/76

        An expired lease used to be free to take, so continuing somebody's
        abandoned hunt was the default and the operator was the only gate on
        whether it was the right hunt to continue at all.
        """
        self.record["lease"]["expires_at"] = (
            datetime.now(UTC) - timedelta(minutes=1)
        ).isoformat()
        with self.assertRaisesRegex(ValueError, "abandoned by first"):
            hunt.require_lease(self.record, "second")
        with self.assertRaisesRegex(ValueError, "decision, not a default"):
            hunt.acquire_lease(self.root, self.record, owner="second")
        hunt.acquire_lease(self.root, self.record, owner="second",
                           takeover_reason="first task never came back")
        self.assertFalse(self.record["lease"]["takeovers"][-1]["was_live"])

    def test_the_owner_may_resume_its_own_abandoned_hunt(self):
        self.record["lease"]["expires_at"] = (
            datetime.now(UTC) - timedelta(minutes=1)
        ).isoformat()
        hunt.require_lease(self.record, "first")
        hunt.acquire_lease(self.root, self.record, owner="first")
        self.assertEqual(self.record["lease"]["takeovers"], [])

    def test_an_abandoned_hunt_can_be_closed_without_adopting_it(self):
        """https://github.com/wolfgang-aura/Mailman/issues/77"""
        self.record["lease"]["expires_at"] = (
            datetime.now(UTC) - timedelta(minutes=1)
        ).isoformat()
        hunt.abandon(self.root, self.record, reason="its targets were not mine",
                     owner="second")
        self.assertEqual(self.record["status"], "ABANDONED")
        self.assertTrue(hunt.is_terminal(self.record))

    def test_a_hunt_recorded_before_leases_existed_still_works(self):
        self.record.pop("lease")
        hunt.require_lease(self.record, None)
        self.assertFalse(hunt.lease_state(self.record)["held"])


class ArtifactSizeTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/66"""

    def test_a_long_stream_keeps_its_head_and_tail(self):
        text = "start" + ("x" * 500_000) + "end"
        trimmed = truncate_stream(text, limit=1000)
        self.assertTrue(trimmed.startswith("start"))
        self.assertTrue(trimmed.endswith("end"))
        self.assertIn("elided", trimmed)
        self.assertLess(len(trimmed), 1200)

    def test_a_short_stream_is_untouched(self):
        self.assertEqual(truncate_stream("brief"), "brief")

    def test_an_oversized_step_moves_to_its_own_file(self):
        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "target-assessment.json"
            data = {"searched": True, "intel": "y" * (STEP_BYTE_LIMIT + 1)}
            summary = offload(data, destination, keep=("searched",))
            self.assertEqual(summary["searched"], True)
            self.assertNotIn("intel", summary)
            self.assertEqual(summary["offloaded_to"], str(destination))
            self.assertGreater(summary["offloaded_bytes"], STEP_BYTE_LIMIT)
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8"))["intel"],
                data["intel"],
            )

    def test_a_small_step_stays_inline(self):
        with TemporaryDirectory() as temporary:
            destination = Path(temporary) / "unused.json"
            data = {"searched": True, "intel": "short"}
            self.assertEqual(offload(data, destination, keep=("searched",)), data)
            self.assertFalse(destination.exists())

    def test_a_recorded_command_result_caps_its_streams(self):
        from mailman.executor import CommandResult

        result = CommandResult(
            command=("echo",), working_directory=".", started_at="now",
            duration_seconds=0.0, exit_code=0, stdout="z" * 400_000,
            stderr="", timed_out=False, timeout_seconds=1.0, environment={},
        )
        self.assertIn("elided", str(result.to_dict()["stdout"]))
        self.assertEqual(result.stdout, "z" * 400_000)


class HealthTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/67"""

    def test_a_provider_usage_limit_is_not_a_candidate_failure(self):
        self.assertEqual(
            health.classify("You've hit your usage limit. Try again at 3:10 AM."),
            health.USAGE_LIMIT,
        )
        self.assertEqual(health.classify("HTTP 429 too many requests"),
                         health.USAGE_LIMIT)

    def test_a_host_failure_is_classified_apart_from_the_code(self):
        self.assertEqual(
            health.classify("PermissionError: [Errno 13] cannot write cache"),
            health.INFRASTRUCTURE,
        )

    def test_an_ordinary_test_failure_is_neither(self):
        self.assertIsNone(health.classify("assert 1 == 2\n1 failed"))
        self.assertIsNone(health.classify(None, ""))

    def test_the_record_carries_the_exact_resume_command(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            health.record(directory, state=health.USAGE_LIMIT, stage="agent:reviewer",
                          resume_command="mailman resume-review RUN", detail="stopped")
            loaded = health.load(directory)
            self.assertEqual(loaded["state"], health.USAGE_LIMIT)
            self.assertEqual(loaded["resume_command"], "mailman resume-review RUN")
            health.clear(directory)
            self.assertIsNone(health.load(directory))


if __name__ == "__main__":
    unittest.main()


class NarrowFirstDuplicateSearchTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/68"""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.calls: list[list[str]] = []

    def _patch(self, payloads):
        from mailman import submission
        from mailman.executor import CommandResult

        def fake_execute(command, *, working_directory, timeout_seconds):
            self.calls.append(list(command))
            method = "narrow" if "--search" in command and "--state" in command and command[-5] == "--limit" else None
            return CommandResult(
                command=tuple(command), working_directory=str(working_directory),
                started_at="now", duration_seconds=0.0, exit_code=0,
                stdout=json.dumps(payloads(list(command))), stderr="",
                timed_out=False, timeout_seconds=timeout_seconds, environment={},
            )

        original = submission.execute
        submission.execute = fake_execute
        self.addCleanup(lambda: setattr(submission, "execute", original))

    def test_the_narrow_query_runs_before_the_broad_listing(self):
        self._patch(lambda command: [])
        from mailman.submission import record_duplicate_search

        record_duplicate_search(
            self.directory, repository="https://github.com/example/project.git",
            query="show dependency sources", issue_number=10967,
            executable="gh", symbols=("_display_packages_information",),
        )
        first = self.calls[0]
        self.assertIn("--search", first)
        self.assertIn("#10967 _display_packages_information", first)
        methods = [call for call in self.calls if "--state" in call and "open" in call]
        self.assertTrue(methods, "the broad listing still runs when nothing is found")
        self.assertGreater(self.calls.index(methods[0]), 0)

    def test_a_confirmed_duplicate_stops_the_expensive_methods(self):
        hit = [{"number": 42, "title": "Add sources to show", "state": "OPEN",
                "url": "https://example.invalid/42", "createdAt": "2026-01-01"}]
        self._patch(lambda command: hit if "--search" in command else [])
        from mailman.submission import record_duplicate_search

        record = record_duplicate_search(
            self.directory, repository="https://github.com/example/project.git",
            query="show dependency sources", issue_number=10967, executable="gh",
        )
        self.assertEqual(record["decided_by"], "narrow")
        self.assertTrue(record["success"])
        self.assertTrue(record["complete"])
        self.assertEqual(record["match_count"], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("10967", record["detail"])

    def test_a_search_with_nothing_narrow_to_go_on_is_unchanged(self):
        self._patch(lambda command: [])
        from mailman.submission import record_duplicate_search

        record = record_duplicate_search(
            self.directory, repository="https://github.com/example/project.git",
            query="show dependency sources", executable="gh",
        )
        self.assertEqual(record["decided_by"], "broad")
        self.assertNotIn("narrow", record["methods"]["pr"])


class VerificationPassthroughTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/70"""

    def test_a_mailman_option_after_the_separator_is_refused(self):
        from mailman.cli import check_verification_command

        for command in (
            ["pytest", "-q", "--data-root", "runs"],
            ["pytest", "--max-review-cycles=2"],
            ["python", "-m", "pytest", "--reasoning-effort", "max"],
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValueError, "Mailman option"):
                    check_verification_command(command)

    def test_a_command_that_starts_with_an_option_is_refused(self):
        from mailman.cli import check_verification_command

        with self.assertRaisesRegex(ValueError, "must be an executable"):
            check_verification_command(["-q", "tests"])

    def test_a_real_verification_command_passes(self):
        from mailman.cli import check_verification_command

        check_verification_command(
            ["python", "-m", "pytest", "tests/test_show.py", "-q", "-p", "no:cacheprovider"]
        )
        check_verification_command([])

    def test_the_cli_reports_it_rather_than_running_the_runner(self):
        from contextlib import redirect_stderr
        from io import StringIO

        from mailman.cli import main

        stderr = StringIO()
        with redirect_stderr(stderr):
            code = main(["build-prompts", "RUN", "--", "pytest", "--data-root", "x"])
        self.assertNotEqual(code, 0)
        self.assertIn("Mailman option", stderr.getvalue())
