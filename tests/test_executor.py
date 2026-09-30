from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailman.executor import StopExecution, execute, reset_deadline, set_deadline


class ExecutorTests(unittest.TestCase):
    def test_a_finished_command_leaves_no_pipe_open(self) -> None:
        """https://github.com/wolfgang-aura/Mailman/issues/281"""
        import gc
        import warnings

        with tempfile.TemporaryDirectory() as temporary_directory:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                result = execute(
                    [sys.executable, "-c", "print('out')"],
                    working_directory=Path(temporary_directory),
                    timeout_seconds=30,
                )
                gc.collect()

        self.assertEqual(result.stdout.strip(), "out")
        self.assertEqual(
            [str(w.message) for w in caught if w.category is ResourceWarning], []
        )

    def test_active_hunt_deadline_clamps_a_command_timeout(self) -> None:
        token = set_deadline(
            datetime.now(UTC) + timedelta(seconds=0.1), label="hunt fixture"
        )
        try:
            with tempfile.TemporaryDirectory() as temporary_directory:
                result = execute(
                    [sys.executable, "-c", "import time; time.sleep(2)"],
                    working_directory=Path(temporary_directory),
                    timeout_seconds=5,
                )
        finally:
            reset_deadline(token)

        self.assertTrue(result.timed_out)
        self.assertLess(result.timeout_seconds, 0.5)

    def test_expired_hunt_deadline_refuses_to_launch_a_command(self) -> None:
        token = set_deadline(
            datetime.now(UTC) - timedelta(seconds=1), label="hunt fixture"
        )
        try:
            with (
                tempfile.TemporaryDirectory() as temporary_directory,
                self.assertRaisesRegex(ValueError, "hunt fixture deadline expired"),
            ):
                execute(
                    [sys.executable, "-c", "print('must not run')"],
                    working_directory=Path(temporary_directory),
                )
        finally:
            reset_deadline(token)

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

    def test_a_timeout_stops_a_grandchild_holding_the_output_pipe(self) -> None:
        # nicegui run 20260930T113729Z-2bd5a4: the venv launcher's pytest
        # child held stdout open through a stuck chromedriver. subprocess.run
        # killed only the launcher and waited on the pipe for two hours.
        # Mailman #276.
        script = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
            "stdout=sys.stdout, stderr=sys.stderr); time.sleep(30)"
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute(
                [sys.executable, "-c", script],
                working_directory=Path(temporary_directory),
                timeout_seconds=1,
            )

        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 15)


class VenvActivationTests(unittest.TestCase):
    def test_a_venv_interpreter_gets_its_scripts_folder_on_path(self) -> None:
        # prefect's hosted_api_server fixture spawns bare `uvicorn`; without
        # the venv's scripts folder on PATH it found none and the baseline
        # failed. Mailman #246.
        import os
        import venv

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            venv.create(root / "env", with_pip=False)
            scripts = root / "env" / ("Scripts" if os.name == "nt" else "bin")
            interpreter = scripts / ("python.exe" if os.name == "nt" else "python")
            result = execute(
                [
                    str(interpreter),
                    "-c",
                    "import os; print(os.environ['PATH'].split(os.pathsep)[0]);"
                    " print(os.environ.get('VIRTUAL_ENV', ''))",
                ],
                working_directory=root,
            )

        self.assertEqual(result.exit_code, 0, result.stderr)
        first, virtual_env = result.stdout.splitlines()[:2]
        self.assertEqual(Path(first), scripts)
        self.assertEqual(Path(virtual_env), root / "env")

    def test_a_host_interpreter_leaves_path_alone(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = execute(
                ["python", "-c", "import os; print(os.environ['PATH'])"],
                working_directory=Path(temporary_directory),
            )

        self.assertEqual(result.stdout.strip(), os.environ["PATH"])


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

    def test_a_deliberate_stream_stop_kills_the_process_and_records_why(self) -> None:
        seen: list[str] = []

        def stop_after_first_line(line: str) -> None:
            seen.append(line)
            raise StopExecution(
                "command budget exceeded: 2 commands attempted, budget 1"
            )

        script = (
            "import time\n"
            "print('command two', flush=True)\n"
            "time.sleep(30)\n"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            start = time.monotonic()
            result = execute(
                [sys.executable, "-c", script],
                working_directory=Path(temporary_directory),
                timeout_seconds=20,
                on_stdout_line=stop_after_first_line,
            )

        self.assertEqual(seen, ["command two"])
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.exit_code)
        self.assertEqual(
            result.stopped_reason,
            "command budget exceeded: 2 commands attempted, budget 1",
        )
        self.assertLess(time.monotonic() - start, 5)

    def test_a_stream_stop_kills_descendants_that_hold_the_output_pipe(self) -> None:
        def stop_after_ready(line: str) -> None:
            if line == "ready":
                raise StopExecution("stage stopped")

        child = "import time; time.sleep(20)"
        script = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "print('ready', flush=True)\n"
            "time.sleep(20)\n"
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            start = time.monotonic()
            result = execute(
                [sys.executable, "-c", script],
                working_directory=Path(temporary_directory),
                timeout_seconds=15,
                on_stdout_line=stop_after_ready,
            )

        self.assertEqual(result.stopped_reason, "stage stopped")
        self.assertLess(time.monotonic() - start, 5)


if __name__ == "__main__":
    unittest.main()
