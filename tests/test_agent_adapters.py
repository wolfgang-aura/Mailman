from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailman.agents.base import AgentRequest, resolve_executable
from mailman.agents.claude_cli import ClaudeCliAgent
from mailman.agents.codex_cli import CodexCliAgent
from mailman.executor import CommandResult


class AgentAdapterTests(unittest.TestCase):
    def test_codex_does_not_count_unchanged_placeholder_as_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prompt = root / "prompt.md"
            prompt.write_text("inspect the fixture", encoding="utf-8")
            report = root / "primary-report.md"
            report.write_text("# Primary report\n\nNot started.\n", encoding="utf-8")
            request = AgentRequest(
                run_id="run-1",
                role="primary",
                prompt_path=prompt,
                workspace=root,
                report_path=report,
                timeout_seconds=60,
            )
            process = CommandResult(
                command=["codex", "exec"],
                working_directory=str(root),
                started_at="2026-09-02T00:00:00+00:00",
                duration_seconds=0.1,
                exit_code=1,
                stdout="",
                stderr="failed",
                timed_out=False,
                timeout_seconds=60,
                environment={},
            )
            with patch("mailman.agents.codex_cli.execute", return_value=process):
                result = CodexCliAgent(
                    executable=sys.executable, windows_sandbox=None
                ).run(request)

        self.assertFalse(result.report_present)

    def test_missing_executable_names_the_tool_and_the_fix(self) -> None:
        with self.assertRaisesRegex(FileNotFoundError, "probe-tool --name codex"):
            resolve_executable("codex-that-is-not-installed")

    def test_an_explicit_executable_path_is_resolved(self) -> None:
        self.assertTrue(Path(resolve_executable(sys.executable)).is_file())

    def test_an_explicit_missing_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing = Path(temporary_directory) / "nested" / "codex"
            with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
                resolve_executable(str(missing))

    def test_codex_command_is_ephemeral_and_sandboxed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prompt = root / "prompt.md"
            prompt.write_text("sensitive issue text", encoding="utf-8")
            request = AgentRequest(
                run_id="run-1",
                role="primary",
                prompt_path=prompt,
                workspace=root,
                report_path=root / "primary-report.md",
                timeout_seconds=60,
            )
            command = CodexCliAgent(
                model="gpt-5.6-sol", windows_sandbox="elevated"
            ).build_command(request)

        self.assertEqual(command[:2], ["codex", "exec"])
        self.assertIn("--ephemeral", command)
        self.assertIn("workspace-write", command)
        self.assertIn("--output-last-message", command)
        self.assertIn("windows.sandbox='elevated'", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertNotIn("sensitive issue text", command)
        self.assertEqual(command[-1], "-")

    def test_codex_reviewer_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request = AgentRequest(
                run_id="run-1",
                role="reviewer",
                prompt_path=root / "prompt.md",
                workspace=root,
                report_path=root / "review-report.md",
            )
            command = CodexCliAgent(windows_sandbox=None).build_command(request)

        sandbox_index = command.index("--sandbox")
        self.assertEqual(command[sandbox_index + 1], "read-only")

    def test_codex_rejects_unknown_windows_sandbox_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request = AgentRequest(
                run_id="run-1",
                role="primary",
                prompt_path=root / "prompt.md",
                workspace=root,
                report_path=root / "primary-report.md",
            )
            with self.assertRaisesRegex(ValueError, "Windows sandbox mode"):
                CodexCliAgent(windows_sandbox="disabled").build_command(request)

    def test_claude_command_is_bounded_and_does_not_bypass_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            prompt = root / "prompt.md"
            prompt.write_text("sensitive review text", encoding="utf-8")
            request = AgentRequest(
                run_id="run-1",
                role="reviewer",
                prompt_path=prompt,
                workspace=root,
                report_path=root / "review-report.md",
                timeout_seconds=60,
            )
            command = ClaudeCliAgent(model="sonnet", max_turns=12).build_command(request)

        self.assertEqual(command[:2], ["claude", "--print"])
        self.assertIn("--output-format", command)
        self.assertIn("plan", command)
        self.assertIn("12", command)
        self.assertIn("--disallowedTools", command)
        self.assertNotIn("--dangerously-skip-permissions", command)
        self.assertNotIn("sensitive review text", command)

    def test_claude_primary_can_edit_without_bypassing_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            request = AgentRequest(
                run_id="run-1",
                role="primary",
                prompt_path=root / "prompt.md",
                workspace=root,
                report_path=root / "primary-report.md",
            )
            command = ClaudeCliAgent().build_command(request)

        permission_index = command.index("--permission-mode")
        self.assertEqual(command[permission_index + 1], "acceptEdits")
        self.assertNotIn("--dangerously-skip-permissions", command)


if __name__ == "__main__":
    unittest.main()
