"""`mailman package` chains the steps between engineering and filing.

https://github.com/wolfgang-aura/Mailman/issues/167
"""

import argparse
import subprocess
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from mailman.artifacts import create_run
from mailman.identity import Identity
from mailman.package import changed_paths, commit_candidate, run_stages

IDENTITY = Identity("Fixture", "1+fixture@users.noreply.github.com")


def git(workspace: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *arguments], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


class ChangedPathsTests(unittest.TestCase):
    def test_every_path_in_the_diff_is_named_once(self) -> None:
        diff = (
            "diff --git a/pkg/mod.py b/pkg/mod.py\n--- a/pkg/mod.py\n"
            "diff --git a/old.py b/new.py\nrename from old.py\n"
            "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        )

        self.assertEqual(changed_paths(diff), ["pkg/mod.py", "old.py", "new.py"])


class CommitCandidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        git(self.workspace, "init", "-q", "-b", "main")
        (self.workspace / "mod.py").write_text("x = 1\n", encoding="utf-8")
        git(self.workspace, "add", "mod.py")
        git(self.workspace, "-c", "user.name=Base", "-c", "user.email=base@example.com",
            "commit", "-q", "-m", "base")
        self.base = git(self.workspace, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def commit(self) -> str:
        return commit_candidate(
            self.workspace, base_commit=self.base, branch="mailman/issue-7",
            message="Fix the thing", identity=IDENTITY,
            paths=["mod.py", "tests/test_mod.py"],
        )

    def test_only_exported_paths_are_committed_under_the_identity(self) -> None:
        (self.workspace / "mod.py").write_text("x = 2\n", encoding="utf-8")
        (self.workspace / "tests").mkdir()
        (self.workspace / "tests" / "test_mod.py").write_text("assert 1\n", encoding="utf-8")
        (self.workspace / "repro-output.txt").write_text("noise\n", encoding="utf-8")

        head = self.commit()

        self.assertEqual(git(self.workspace, "branch", "--show-current"), "mailman/issue-7")
        self.assertEqual(
            git(self.workspace, "show", "--name-only", "--format=", head).split(),
            ["mod.py", "tests/test_mod.py"],
        )
        self.assertEqual(git(self.workspace, "log", "-1", "--format=%an <%ae>"),
                         "Fixture <1+fixture@users.noreply.github.com>")
        self.assertIn("repro-output.txt", git(self.workspace, "status", "--porcelain"))

    def test_a_second_package_run_keeps_the_same_commit(self) -> None:
        (self.workspace / "mod.py").write_text("x = 2\n", encoding="utf-8")
        first = self.commit()

        self.assertEqual(self.commit(), first)

    def test_nothing_to_commit_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "no commit on top"):
            self.commit()


class RunStagesTests(unittest.TestCase):
    def test_it_stops_at_the_first_failure_and_names_it(self) -> None:
        ran = []

        def stage(name: str, code: int):
            return name, lambda: ran.append(name) or code

        stream = StringIO()
        code, record = run_stages(
            [stage("a", 0), stage("b", 1), stage("c", 0)], stream=stream
        )

        self.assertEqual(code, 1)
        self.assertEqual(ran, ["a", "b"])
        self.assertEqual([row["stage"] for row in record], ["a", "b"])
        self.assertIn("stopped at b (exit 1)", stream.getvalue())

    def test_a_refusal_is_a_failed_stage_not_a_crash(self) -> None:
        def refuse() -> int:
            raise ValueError("candidate-changed")

        stream = StringIO()
        code, _ = run_stages([("finalize-review", refuse)], stream=stream)

        self.assertEqual(code, 2)
        self.assertIn("candidate-changed", stream.getvalue())


class PackageCommandTests(unittest.TestCase):
    def test_the_stages_run_in_the_procedure_order(self) -> None:
        from mailman import cli

        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, _ = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40, primary="codex", reviewer="claude",
                data_root=data_root,
            )
            arguments = argparse.Namespace(
                run_id=run.run_id, data_root=data_root, policy=Path("policy.json"),
                title="Fix it", body=Path("body.md"), repo="example/project",
                head="fork:mailman/issue-7", base="main", commit_message=None,
            )
            (data_root / run.run_id / "decision.json").write_text("{}", encoding="utf-8")
            export = data_root / run.run_id / "export"
            export.mkdir(parents=True, exist_ok=True)
            (export / "changes.diff").write_text("diff --git a/m.py b/m.py\n", encoding="utf-8")
            calls = []
            with (
                patch.object(cli, "main", side_effect=lambda argv: calls.append(argv[0]) or 0),
                patch("mailman.cli.resolve_identity", return_value=IDENTITY),
                patch("mailman.package.commit_candidate", return_value="b" * 40) as committed,
                patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()),
            ):
                code = cli._package(arguments)

        self.assertEqual(code, 0)
        self.assertEqual(calls, [
            "export-patch", "prepare-submission", "decision", "finalize-review",
            "check-authors", "handoff", "handoff-check", "review",
        ])
        self.assertEqual(committed.call_args.kwargs["branch"], "mailman/issue-7")
        self.assertEqual(committed.call_args.kwargs["paths"], ["m.py"])

    def test_a_missing_decision_stops_before_any_stage(self) -> None:
        # pyinstaller run 20260929T022154Z-8fc17e exported and prepared the
        # submission, then stopped at the decision stage. Mailman #186.
        from mailman import cli

        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, _ = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40, primary="codex", reviewer="claude",
                data_root=data_root,
            )
            arguments = argparse.Namespace(
                run_id=run.run_id, data_root=data_root, policy=Path("policy.json"),
                title="Fix it", body=Path("body.md"), repo="example/project",
                head="fork:mailman/issue-7", base="main", commit_message=None,
            )
            calls = []
            with (
                patch.object(cli, "main", side_effect=lambda argv: calls.append(argv[0]) or 0),
                self.assertRaisesRegex(ValueError, "decision .* --init"),
            ):
                cli._package(arguments)

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
