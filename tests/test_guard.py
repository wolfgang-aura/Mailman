import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mailman.guard import added_test_names, guard_findings, run_guard


def _git(workspace: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


FIXED = "def double(x):\n    return x * 2\n"
BROKEN = "def double(x):\n    return x + 2\n"


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_directory = Path(tempfile.mkdtemp())
        self.workspace = self.run_directory / "workspace"
        (self.workspace / "tests").mkdir(parents=True)
        (self.workspace / "mod.py").write_text(BROKEN, encoding="utf-8")
        (self.workspace / "tests" / "test_mod.py").write_text(
            "from mod import double\n\n\ndef test_old():\n    assert double(2) == 4\n",
            encoding="utf-8",
        )
        _git(self.workspace, "init", "-q")
        _git(self.workspace, "-c", "user.email=a@b", "-c", "user.name=a", "add", ".")
        _git(self.workspace, "-c", "user.email=a@b", "-c", "user.name=a",
             "commit", "-q", "-m", "base")
        self.base = _git(self.workspace, "rev-parse", "HEAD")
        (self.workspace / "mod.py").write_text(FIXED, encoding="utf-8")

    def _add_tests(self, body: str) -> str:
        path = self.workspace / "tests" / "test_mod.py"
        path.write_text(path.read_text(encoding="utf-8") + body, encoding="utf-8")
        return _git(self.workspace, "diff")

    def _run(self, diff: str) -> dict:
        return run_guard(
            self.run_directory,
            diff=diff,
            changed_paths=["mod.py", "tests/test_mod.py"],
            workspace=self.workspace,
            base_commit=self.base,
            verification_command=[sys.executable, "-m", "pytest", "-q", "tests/test_mod.py"],
        )

    def test_a_new_test_that_passes_without_the_fix_is_named(self) -> None:
        diff = self._add_tests(
            "\n\ndef test_guards():\n    assert double(3) == 6\n"
            "\n\ndef test_unguarded():\n    assert double(0) != 1\n"
        )

        record = self._run(diff)

        self.assertEqual(record["failed_at_base"], ["test_guards"])
        self.assertEqual(record["passed_at_base"], ["test_unguarded"])
        # The candidate's source is back in place.
        self.assertEqual((self.workspace / "mod.py").read_text(encoding="utf-8"), FIXED)
        findings = guard_findings(record)
        self.assertEqual([(f["code"], f["blocking"]) for f in findings],
                         [("unguarded-tests", False)])

    def test_new_tests_that_all_pass_without_the_fix_block(self) -> None:
        diff = self._add_tests("\n\ndef test_unguarded():\n    assert double(0) != 1\n")

        record = self._run(diff)

        self.assertEqual(record["passed_at_base"], ["test_unguarded"])
        self.assertEqual([(f["code"], f["blocking"]) for f in guard_findings(record)],
                         [("new-tests-pass-at-base", True)])

    def test_a_diff_that_adds_no_test_has_nothing_to_guard(self) -> None:
        record = self._run(_git(self.workspace, "diff"))

        self.assertEqual(record["reason"], "no-added-tests")
        self.assertEqual(guard_findings(record), [])

    def test_added_test_names_reads_functions_and_methods(self) -> None:
        diff = (
            "diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
            "@@ -1,2 +1,6 @@\n"
            "+def test_one():\n"
            "+    async def test_not_top_level_but_counted(): pass\n"
            "+    def helper(): pass\n"
            " def test_existing():\n"
            "diff --git a/src/x.py b/src/x.py\n"
            "--- a/src/x.py\n+++ b/src/x.py\n"
            "+def test_in_source(): pass\n"
        )

        self.assertEqual(
            added_test_names(diff),
            {"tests/test_x.py": ["test_one", "test_not_top_level_but_counted"]},
        )

    def test_added_test_names_reads_pytest_mypy_plugins_yml_cases(self) -> None:
        diff = (
            "diff --git a/tests/typecheck/test_m.yml b/tests/typecheck/test_m.yml\n"
            "--- a/tests/typecheck/test_m.yml\n+++ b/tests/typecheck/test_m.yml\n"
            "@@ -1,2 +1,6 @@\n"
            " -   case: existing_case\n"
            "+-   case: new_case\n"
            "+    main: |\n"
            "+- case: \"quoted_case\"\n"
        )

        self.assertEqual(
            added_test_names(diff),
            {"tests/typecheck/test_m.yml": ["new_case", "quoted_case"]},
        )


if __name__ == "__main__":
    unittest.main()
