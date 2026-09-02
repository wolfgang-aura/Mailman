from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from mailman.executor import execute


class ExecutorTests(unittest.TestCase):
    def test_passes_input_without_putting_it_in_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute(
                [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.stdin.read().upper())",
                ],
                working_directory=Path(temporary_directory),
                timeout_seconds=5,
                stdin_text="private prompt",
            )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), "PRIVATE PROMPT")
        self.assertNotIn("private prompt", " ".join(result.command))

    def test_captures_exit_code_and_redacts_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_token = "ghp_" + "a" * 30
            result = execute(
                [
                    sys.executable,
                    "-c",
                    f"import sys; print('{fake_token}'); "
                    "print('failed', file=sys.stderr); raise SystemExit(7)",
                ],
                working_directory=Path(temporary_directory),
                timeout_seconds=5,
            )

        self.assertEqual(result.exit_code, 7)
        self.assertFalse(result.timed_out)
        self.assertIn("[REDACTED_GITHUB_TOKEN]", result.stdout)
        self.assertNotIn(fake_token, result.stdout)
        self.assertEqual(result.stderr.strip(), "failed")
        self.assertNotIn("PATH", result.environment)

    def test_marks_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute(
                [sys.executable, "-c", "import time; time.sleep(2)"],
                working_directory=Path(temporary_directory),
                timeout_seconds=0.05,
            )

        self.assertIsNone(result.exit_code)
        self.assertTrue(result.timed_out)


class StreamingTests(unittest.TestCase):
    def test_hands_over_each_line_before_the_process_exits(self) -> None:
        seen: list[tuple[str, float]] = []
        script = (
            "import sys, time\n"
            "for index in range(3):\n"
            "    print(index, flush=True)\n"
            "    time.sleep(0.2)\n"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            start = time.monotonic()
            result = execute(
                [sys.executable, "-c", script],
                working_directory=Path(temporary_directory),
                timeout_seconds=10,
                on_stdout_line=lambda line: seen.append(
                    (line, time.monotonic() - start)
                ),
            )

        self.assertEqual([line for line, _ in seen], ["0", "1", "2"])
        # The first line has to arrive well before the last one, or nothing
        # was streamed and the callback just replayed a buffer.
        self.assertLess(seen[0][1], seen[-1][1] - 0.2)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.split(), ["0", "1", "2"])

    def test_still_passes_stdin_and_captures_stderr_while_streaming(self) -> None:
        seen: list[str] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute(
                [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.stdin.read().upper()); "
                    "print('warned', file=sys.stderr)",
                ],
                working_directory=Path(temporary_directory),
                timeout_seconds=10,
                stdin_text="private prompt",
                on_stdout_line=seen.append,
            )

        self.assertEqual(seen, ["PRIVATE PROMPT"])
        self.assertEqual(result.stderr.strip(), "warned")
        self.assertNotIn("private prompt", " ".join(result.command))

    def test_redacts_the_captured_stream(self) -> None:
        fake_token = "ghp_" + "a" * 30
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute(
                [sys.executable, "-c", f"print('{fake_token}')"],
                working_directory=Path(temporary_directory),
                timeout_seconds=10,
                on_stdout_line=lambda line: None,
            )

        self.assertNotIn(fake_token, result.stdout)

    def test_marks_a_timeout_while_streaming(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                working_directory=Path(temporary_directory),
                timeout_seconds=0.3,
                on_stdout_line=lambda line: None,
            )

        self.assertIsNone(result.exit_code)
        self.assertTrue(result.timed_out)

    def test_a_broken_watcher_does_not_lose_the_evidence(self) -> None:
        def explode(line: str) -> None:
            raise RuntimeError("the console went away")

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute(
                [sys.executable, "-c", "print('still recorded')"],
                working_directory=Path(temporary_directory),
                timeout_seconds=10,
                on_stdout_line=explode,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("still recorded", result.stdout)


if __name__ == "__main__":
    unittest.main()
