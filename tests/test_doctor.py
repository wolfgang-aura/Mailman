from __future__ import annotations

import unittest
from pathlib import Path

from mailman.doctor import describe_interpreter_reach, run_checks


class InterpreterReachTests(unittest.TestCase):
    def test_an_interpreter_under_the_profile_is_named_as_unreachable(self) -> None:
        ok, detail = describe_interpreter_reach(
            Path(r"C:\Users\someone\AppData\Local\Python\pythoncore-3.14-64"),
            Path(r"C:\Users\someone"),
        )

        self.assertFalse(ok)
        self.assertIn("inside the user profile", detail)
        self.assertIn("ProgramData", detail)

    def test_an_interpreter_outside_the_profile_passes(self) -> None:
        ok, detail = describe_interpreter_reach(
            Path(r"C:\ProgramData\mailman-python"), Path(r"C:\Users\someone")
        )

        self.assertTrue(ok)
        self.assertIn("outside the user profile", detail)

    def test_the_check_is_reported_and_is_not_required(self) -> None:
        check = next(
            item for item in run_checks() if item.name == "agent-runnable python"
        )

        self.assertFalse(check.required)


if __name__ == "__main__":
    unittest.main()
