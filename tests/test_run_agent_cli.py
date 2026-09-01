from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mailman.agents.base import AgentRequest, AgentResult, EngineeringAgent
from mailman.artifacts import create_run, load_run
from mailman.cli import main
from mailman.executor import CommandResult
from mailman.models import RunStatus


def git(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        shell=False,
    )
    return completed.stdout.strip()


class FakeAgent(EngineeringAgent):
    @property
    def name(self) -> str:
        return "codex"

    def run(self, request: AgentRequest) -> AgentResult:
        request.report_path.write_text("candidate ready\n", encoding="utf-8")
        command_result = CommandResult(
            command=["fake-agent"],
            working_directory=str(request.workspace),
            started_at="2026-09-02T00:00:00+00:00",
            duration_seconds=0.1,
            exit_code=0,
            stdout="done\n",
            stderr="",
            timed_out=False,
            timeout_seconds=request.timeout_seconds,
            environment={"operating_system": "test"},
        )
        return AgentResult(
            exit_code=0,
            report_path=request.report_path,
            timed_out=False,
            report_present=True,
            command_result=command_result,
        )


class RunAgentCliTests(unittest.TestCase):
    def test_successful_process_does_not_advance_workflow_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            git(workspace, "init", "--initial-branch=main")
            git(workspace, "config", "user.name", "Fixture")
            git(workspace, "config", "user.email", "fixture@example.invalid")
            (workspace / "code.txt").write_text("baseline\n", encoding="utf-8")
            git(workspace, "add", "--", "code.txt")
            git(workspace, "commit", "-m", "baseline")
            base_commit = git(workspace, "rev-parse", "HEAD")
            data_root = root / "runs"
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/1",
                base_commit=base_commit,
                primary="codex",
                reviewer="claude",
                data_root=data_root,
            )
            prompt = root / "prompt.md"
            prompt.write_text("work on the fixture", encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()
            with patch("mailman.cli._make_agent", return_value=FakeAgent()):
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "run-agent",
                            run.run_id,
                            "--role",
                            "primary",
                            "--prompt",
                            str(prompt),
                            "--workspace",
                            str(workspace),
                            "--data-root",
                            str(data_root),
                        ]
                    )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            loaded, _ = load_run(run.run_id, data_root)
            self.assertEqual(loaded.status, RunStatus.INITIALIZED)
            records = list((run_directory / "agent-executions").glob("*.json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(record["process"]["exit_code"], 0)
            self.assertEqual(record["report"], "candidate ready\n")
            self.assertEqual(record["workflow_status_after_run"], "INITIALIZED")

    def test_reviewer_accepts_candidate_commit_descended_from_base(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            git(workspace, "init", "--initial-branch=main")
            git(workspace, "config", "user.name", "Fixture")
            git(workspace, "config", "user.email", "fixture@example.invalid")
            (workspace / "code.txt").write_text("baseline\n", encoding="utf-8")
            git(workspace, "add", "--", "code.txt")
            git(workspace, "commit", "-m", "baseline")
            base_commit = git(workspace, "rev-parse", "HEAD")
            (workspace / "code.txt").write_text("candidate\n", encoding="utf-8")
            git(workspace, "add", "--", "code.txt")
            git(workspace, "commit", "-m", "candidate")
            data_root = root / "runs"
            run, _ = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/1",
                base_commit=base_commit,
                primary="claude",
                reviewer="codex",
                data_root=data_root,
            )
            prompt = root / "prompt.md"
            prompt.write_text("review the fixture", encoding="utf-8")
            stderr = StringIO()
            with patch("mailman.cli._make_agent", return_value=FakeAgent()):
                with redirect_stdout(StringIO()), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "run-agent",
                            run.run_id,
                            "--role",
                            "reviewer",
                            "--prompt",
                            str(prompt),
                            "--workspace",
                            str(workspace),
                            "--data-root",
                            str(data_root),
                        ]
                    )

            self.assertEqual(exit_code, 0, stderr.getvalue())

    def test_wrong_base_commit_stops_before_agent_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            workspace.mkdir()
            git(workspace, "init", "--initial-branch=main")
            git(workspace, "config", "user.name", "Fixture")
            git(workspace, "config", "user.email", "fixture@example.invalid")
            (workspace / "code.txt").write_text("baseline\n", encoding="utf-8")
            git(workspace, "add", "--", "code.txt")
            git(workspace, "commit", "-m", "baseline")
            data_root = root / "runs"
            run, _ = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/1",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                data_root=data_root,
            )
            prompt = root / "prompt.md"
            prompt.write_text("work on the fixture", encoding="utf-8")
            stderr = StringIO()
            with patch("mailman.cli._make_agent") as make_agent:
                with redirect_stdout(StringIO()), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "run-agent",
                            run.run_id,
                            "--role",
                            "primary",
                            "--prompt",
                            str(prompt),
                            "--workspace",
                            str(workspace),
                            "--data-root",
                            str(data_root),
                        ]
                    )

            self.assertEqual(exit_code, 2)
            self.assertIn("does not match base commit", stderr.getvalue())
            make_agent.assert_not_called()


if __name__ == "__main__":
    unittest.main()
