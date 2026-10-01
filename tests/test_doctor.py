from __future__ import annotations

import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from mailman import doctor
from mailman.doctor import describe_interpreter_reach, run_checks


class InterpreterReachTests(unittest.TestCase):
    def test_an_interpreter_under_the_profile_is_named_as_unreachable(self) -> None:
        # Paths are built with the running platform's own separators. CI runs on
        # Linux, where a literal Windows string is one relative path segment and
        # every containment check silently passes.
        home = Path(tempfile.gettempdir()) / "profile"
        ok, detail = describe_interpreter_reach(home / "AppData" / "python", home)

        self.assertFalse(ok)
        self.assertIn("inside the user profile", detail)
        self.assertIn("ProgramData", detail)

    def test_an_interpreter_outside_the_profile_passes(self) -> None:
        root = Path(tempfile.gettempdir())
        ok, detail = describe_interpreter_reach(
            root / "shared" / "python", root / "profile"
        )

        self.assertTrue(ok)
        self.assertIn("outside the user profile", detail)

    def test_the_check_is_reported_and_is_not_required(self) -> None:
        with mock.patch.object(doctor, "_command_version", return_value="stub 1.0"):
            check = next(
                item for item in run_checks() if item.name == "agent-runnable python"
            )

        self.assertFalse(check.required)


class CommandVersionTests(unittest.TestCase):
    def test_a_tool_that_does_not_answer_in_time_is_still_reported(self) -> None:
        def slow(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 10))

        with mock.patch.object(doctor, "find_executable", return_value="C:/tools/codex.CMD"):
            with mock.patch.object(doctor.subprocess, "run", side_effect=slow):
                version = doctor._command_version("codex", ["--version"])

        self.assertIn("codex.CMD", version)
        self.assertIn("no answer", version)

    def test_a_blocked_executable_is_a_failed_check_not_an_abort(self) -> None:
        """Application Control refuses to start a blocked tool with an OSError. #356"""
        def blocked(*args, **kwargs):
            raise OSError(4551, "An Application Control policy has blocked this file")

        with (
            mock.patch.object(doctor, "find_executable", return_value="C:/tools/codex.exe"),
            mock.patch.object(doctor, "_base_interpreter_reachable_by_codex",
                              return_value=(True, "fine")),
            mock.patch.object(doctor, "describe_commit_identity", return_value=(True, "id")),
            mock.patch.object(doctor.subprocess, "run", side_effect=blocked),
        ):
            checks = {check.name: check for check in run_checks()}

        self.assertFalse(checks["codex"].ok)
        self.assertIn("Application Control", checks["codex"].detail)
        self.assertFalse(checks["git"].ok)
        self.assertTrue(checks["git"].required)


if __name__ == "__main__":
    unittest.main()
