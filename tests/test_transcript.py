from __future__ import annotations

import json
import unittest

from mailman.transcript import (
    TranscriptEvent,
    final_message,
    parse_line,
    parse_stream,
    render,
    unwrap_shell,
)


class UnwrapShellTests(unittest.TestCase):
    def test_removes_the_powershell_wrapper_codex_uses_on_windows(self) -> None:
        wrapped = (
            '"C:\\\\Windows\\\\System32\\\\WindowsPowerShell\\\\v1.0\\\\powershell.exe"'
            ' -Command "git status --short"'
        )
        self.assertEqual(unwrap_shell(wrapped), "git status --short")

    def test_removes_a_posix_shell_wrapper(self) -> None:
        self.assertEqual(unwrap_shell("/bin/bash -lc 'pytest -q'"), "pytest -q")

    def test_leaves_a_bare_command_alone(self) -> None:
        self.assertEqual(unwrap_shell("git diff --check"), "git diff --check")

    def test_does_not_strip_a_flag_that_only_looks_like_a_wrapper(self) -> None:
        self.assertEqual(unwrap_shell("git commit -c HEAD"), "git commit -c HEAD")


class CodexStreamTests(unittest.TestCase):
    def test_reports_a_command_and_its_outcome(self) -> None:
        started = json.dumps(
            {
                "type": "item.started",
                "item": {
                    "id": "item_0",
                    "type": "command_execution",
                    "command": "bash -lc 'pytest -q'",
                    "status": "in_progress",
                },
            }
        )
        completed = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "command_execution",
                    "command": "bash -lc 'pytest -q'",
                    "aggregated_output": "collected 3 items\n3 passed",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        )

        events = parse_stream([started, completed], "codex")

        self.assertEqual([event.kind for event in events], ["command", "result"])
        self.assertEqual(events[0].summary, "pytest -q")
        self.assertIn("ok", events[1].summary)
        self.assertIn("3 passed", events[1].summary)
        self.assertIn("collected 3 items", events[1].detail)

    def test_names_a_failing_exit_code(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "pytest",
                    "aggregated_output": "9 failed",
                    "exit_code": 1,
                },
            }
        )

        (event,) = parse_line(line, "codex")

        self.assertEqual(event.kind, "result")
        self.assertIn("exit 1", event.summary)

    def test_shows_file_changes_relative_to_the_workspace(self) -> None:
        line = json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "file_change",
                    "changes": [
                        {
                            "path": "C:\\runs\\abc\\workspace\\src\\raises.py",
                            "kind": "update",
                        }
                    ],
                },
            }
        )

        (event,) = parse_line(line, "codex")

        self.assertEqual(event.kind, "edits")
        self.assertEqual(event.summary, "update src/raises.py")

    def test_reports_token_usage_when_a_turn_completes(self) -> None:
        line = json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 388500, "output_tokens": 3364},
            }
        )

        (event,) = parse_line(line, "codex")

        self.assertEqual(event.kind, "turn")
        self.assertIn("388500 in", event.summary)

    def test_ignores_an_in_progress_message_so_it_is_not_reported_twice(self) -> None:
        line = json.dumps(
            {"type": "item.started", "item": {"type": "agent_message", "text": "hi"}}
        )

        self.assertEqual(parse_line(line, "codex"), [])


class ClaudeStreamTests(unittest.TestCase):
    def test_reports_a_tool_call_and_its_result(self) -> None:
        assistant = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Reproducing the failure first."},
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pytest -q testing/python"},
                        },
                    ]
                },
            }
        )
        result = json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "9 failed, 57 passed",
                            "is_error": False,
                        }
                    ]
                },
            }
        )

        events = parse_stream([assistant, result], "claude")

        self.assertEqual(
            [event.kind for event in events], ["says", "command", "result"]
        )
        self.assertIn("pytest -q testing/python", events[1].summary)
        self.assertIn("9 failed", events[2].summary)

    def test_classifies_a_write_as_an_edit(self) -> None:
        line = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "src/_pytest/raises.py"},
                        }
                    ]
                },
            }
        )

        (event,) = parse_line(line, "claude")

        self.assertEqual(event.kind, "edits")
        self.assertIn("raises.py", event.summary)

    def test_summarizes_the_final_result_with_its_turn_count(self) -> None:
        line = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "num_turns": 12,
                "total_cost_usd": 0.4213,
                "result": "Fixed the callback.",
            }
        )

        (event,) = parse_line(line, "claude")

        self.assertEqual(event.kind, "turn")
        self.assertIn("12 turn(s)", event.summary)
        self.assertIn("$0.4213", event.summary)

    def test_takes_the_last_assistant_message_as_the_report(self) -> None:
        events = [
            TranscriptEvent("says", "first"),
            TranscriptEvent("command", "pytest"),
            TranscriptEvent("says", "summary", "the full summary"),
        ]

        self.assertEqual(final_message(events), "the full summary")

    def test_has_no_report_when_the_agent_never_spoke(self) -> None:
        self.assertIsNone(final_message([TranscriptEvent("command", "pytest")]))


class MalformedStreamTests(unittest.TestCase):
    def test_keeps_a_plain_error_line_that_is_not_json(self) -> None:
        (event,) = parse_line("Error: Reached max turns (30)", "claude")

        self.assertEqual(event.kind, "output")
        self.assertEqual(event.summary, "Error: Reached max turns (30)")

    def test_keeps_truncated_json_instead_of_raising(self) -> None:
        (event,) = parse_line('{"type": "assis', "codex")

        self.assertEqual(event.kind, "output")

    def test_skips_blank_lines(self) -> None:
        self.assertEqual(parse_line("   ", "codex"), [])

    def test_redacts_secrets_when_rendering(self) -> None:
        token = "ghp_" + "a" * 30
        rendered = render([TranscriptEvent("command", f"git push {token}")])

        self.assertNotIn(token, rendered)


if __name__ == "__main__":
    unittest.main()
