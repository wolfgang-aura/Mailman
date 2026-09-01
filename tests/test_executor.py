from __future__ import annotations

import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
