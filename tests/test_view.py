from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.view import agent_executions, render_run, summarize_runs, write_transcript_logs


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _build_run(root: Path, run_id: str = "20260902T110923Z-2eb44e") -> Path:
    run_directory = root / run_id
    _write(
        run_directory / "run.json",
        {
            "run_id": run_id,
            "status": "BLOCKED",
            "repository": "https://github.com/pytest-dev/pytest.git",
            "issue": "https://github.com/pytest-dev/pytest/issues/14324",
        },
    )
    _write(
        run_directory / "orchestration.json",
        {
            "run_id": run_id,
            "final_status": "BLOCKED",
            "revisions_used": 0,
            "review_cycles": 0,
            "steps": [
                {
                    "name": "agent:primary",
                    "at": "2026-09-02T11:13:08.318202+00:00",
                    "ok": True,
                    "detail": "codex finished with exit code 0",
                    "data": {},
                },
                {
                    "name": "verification:primary",
                    "at": "2026-09-02T11:13:16.363379+00:00",
                    "ok": False,
                    "detail": "verification exited with code 1",
                    "data": {},
                },
            ],
        },
    )
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "abc"}),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {
                        "type": "command_execution",
                        "command": "bash -lc 'pytest -q'",
                        "status": "in_progress",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "bash -lc 'pytest -q'",
                        "aggregated_output": "9 failed, 57 passed",
                        "exit_code": 1,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Fixed the callback."},
                }
            ),
        ]
    )
    _write(
        run_directory / "agent-executions" / "0001-primary.json",
        {
            "agent": "codex",
            "role": "primary",
            "report_present": True,
            "process": {"stdout": stdout, "duration_seconds": 136.4, "exit_code": 0},
        },
    )
    (run_directory / "primary-report.md").write_text(
        "# Primary report\n\nVerdict: BLOCKED\n", encoding="utf-8"
    )
    return run_directory


class RenderRunTests(unittest.TestCase):
    def test_shows_the_timeline_and_the_transcript_together(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = _build_run(Path(temporary))

            rendered = render_run(run_directory)

        self.assertIn("20260902T110923Z-2eb44e", rendered)
        self.assertIn("codex finished with exit code 0", rendered)
        self.assertIn("verification exited with code 1", rendered)
        self.assertIn("pytest -q", rendered)
        self.assertIn("9 failed, 57 passed", rendered)
        self.assertIn("Fixed the callback.", rendered)

    def test_marks_a_failed_step_differently_from_a_passing_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rendered = render_run(_build_run(Path(temporary)))

        self.assertIn("ok   agent:primary", rendered)
        self.assertIn("stop verification:primary", rendered)

    def test_reports_the_agent_and_how_long_it_ran(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rendered = render_run(_build_run(Path(temporary)))

        self.assertIn("0001 primary - codex, 2.3m", rendered)

    def test_omits_reports_when_asked_to(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            rendered = render_run(_build_run(Path(temporary)), reports=False)

        self.assertNotIn("Verdict: BLOCKED", rendered)

    def test_full_mode_includes_the_captured_output_body(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = _build_run(Path(temporary))

            summary = render_run(run_directory, width=40)
            full = render_run(run_directory, width=40, full=True)

        self.assertLess(len(summary), len(full))
        self.assertIn("    9 failed, 57 passed", full)

    def test_says_so_when_an_agent_captured_nothing_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = _build_run(Path(temporary))
            _write(
                run_directory / "agent-executions" / "0001-primary.json",
                {"agent": "claude", "role": "primary", "process": {"stdout": ""}},
            )

            rendered = render_run(run_directory)

        self.assertIn("no machine-readable output was captured", rendered)

    def test_renders_a_run_that_never_reached_orchestration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = Path(temporary) / "20260902T112525Z-ffc2f3"
            _write(
                run_directory / "run.json",
                {"run_id": "20260902T112525Z-ffc2f3", "status": "INITIALIZED"},
            )

            rendered = render_run(run_directory)

        self.assertIn("INITIALIZED", rendered)
        self.assertNotIn("timeline", rendered)


class TranscriptLogTests(unittest.TestCase):
    def test_writes_a_log_beside_the_execution_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = _build_run(Path(temporary))

            written = write_transcript_logs(run_directory)

        self.assertEqual([path.name for path in written], ["0001-primary.log"])

    def test_finds_every_execution_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = _build_run(Path(temporary))
            _write(
                run_directory / "agent-executions" / "0002-reviewer.json",
                {"agent": "claude", "role": "reviewer", "process": {"stdout": ""}},
            )

            executions = agent_executions(run_directory)

        self.assertEqual([one.role for one in executions], ["primary", "reviewer"])
        self.assertEqual([one.number for one in executions], [1, 2])


class SummaryTests(unittest.TestCase):
    def test_lists_each_run_with_its_status_and_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_directory = _build_run(Path(temporary))

            summary = summarize_runs([run_directory])

        self.assertIn("20260902T110923Z-2eb44e", summary)
        self.assertIn("BLOCKED", summary)
        self.assertIn("primary:codex", summary)


if __name__ == "__main__":
    unittest.main()
