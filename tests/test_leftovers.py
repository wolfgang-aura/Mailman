from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest import mock

from mailman.executor import execute
from mailman.leftovers import leftover_processes, match_run_environments

RUN_ID = "20260930T032102Z-78a763"


class MatchTests(unittest.TestCase):
    def test_only_an_executable_under_a_run_environment_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            environment = root / RUN_ID / "environment" / "Scripts" / "prefect.exe"
            rows = [
                {"ProcessId": 1, "ExecutablePath": str(environment),
                 "CommandLine": "prefect server start --port 2229"},
                {"ProcessId": 2, "ExecutablePath": str(root / RUN_ID / "workspace" / "x.exe")},
                {"ProcessId": 3, "ExecutablePath": str(Path(directory) / "environment" / "y.exe")},
                {"ProcessId": 4, "ExecutablePath": None},
            ]
            if os.name == "nt":
                rows.append({"ProcessId": 5, "ExecutablePath": str(environment).upper()})

            found = match_run_environments(rows, root)

        self.assertEqual([row["pid"] for row in found], [1, 5] if os.name == "nt" else [1])
        self.assertEqual(found[0]["run_id"], RUN_ID)
        self.assertEqual(found[0]["command_line"], "prefect server start --port 2229")

    def test_hunt_status_names_leftovers_and_a_failed_check(self) -> None:
        from mailman.cli import _report_leftover_processes

        row = {"run_id": RUN_ID, "pid": 29068, "executable": "x", "command_line": ""}
        result: dict = {}
        with mock.patch("mailman.leftovers.leftover_processes", return_value=([row], None)):
            _report_leftover_processes(Path("runs"), result)
        self.assertEqual(result["leftover_processes"], [row])
        self.assertIn("taskkill", result["leftover_processes_action"])

        result = {}
        with mock.patch("mailman.leftovers.leftover_processes",
                        return_value=([], "process query failed: timed out")):
            _report_leftover_processes(Path("runs"), result)
        self.assertEqual(result, {"leftover_processes_unchecked": "process query failed: timed out"})

        result = {}
        with mock.patch("mailman.leftovers.leftover_processes", return_value=([], None)):
            _report_leftover_processes(Path("runs"), result)
        self.assertEqual(result, {})


@unittest.skipUnless(sys.platform == "win32", "Win32_Process query")
class LiveQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "runs"
        environment = self.root / RUN_ID / "environment"
        venv.create(environment, with_pip=False)
        self.interpreter = environment / "Scripts" / "python.exe"

    def tearDown(self) -> None:
        found, _ = leftover_processes(self.root)
        for process in found:
            subprocess.run(["taskkill", "/PID", str(process["pid"]), "/T", "/F"],
                           capture_output=True, check=False)
        self.directory.cleanup()

    def test_a_process_running_from_a_run_environment_is_reported(self) -> None:
        process = subprocess.Popen(
            [str(self.interpreter), "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            found, failure = leftover_processes(self.root)
        finally:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, check=False)
            process.wait(timeout=10)

        self.assertIsNone(failure)
        self.assertIn(process.pid, [row["pid"] for row in found])
        self.assertEqual({row["run_id"] for row in found}, {RUN_ID})

    def test_a_step_leaves_nothing_running_from_the_environment(self) -> None:
        # The prefect shape: a step starts a server from the environment in
        # the background and exits. Mailman #277.
        script = (
            "import subprocess, sys\n"
            "subprocess.Popen(\n"
            "    [sys.argv[1], '-c', 'import time; time.sleep(60)'],\n"
            "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
            "    stderr=subprocess.DEVNULL,\n"
            "    creationflags=subprocess.DETACHED_PROCESS\n"
            "    | subprocess.CREATE_NEW_PROCESS_GROUP,\n"
            ")\n"
        )
        result = execute(
            [sys.executable, "-c", script, str(self.interpreter)],
            working_directory=self.root,
            timeout_seconds=30,
        )

        self.assertEqual(result.exit_code, 0, result.stderr)
        found, failure = leftover_processes(self.root)
        self.assertIsNone(failure)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
