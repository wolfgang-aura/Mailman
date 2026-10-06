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
from mailman.package import (
    changed_paths, check_signoff, commit_candidate, run_stages, signoff_requirement,
)

IDENTITY = Identity("Fixture", "1+fixture@users.noreply.github.com")


def git(workspace: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(workspace), *arguments], check=True,
        capture_output=True, text=True,
    ).stdout.strip()


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

    def test_quoted_and_ambiguous_paths_are_listed_and_committed(self) -> None:
        """A diff header C-quotes `café.py` and cannot be split for `x b/y.py`. #338"""
        for name in ("café.py", "x b/y.py", "old.py"):
            (self.workspace / name).parent.mkdir(parents=True, exist_ok=True)
            (self.workspace / name).write_text("a = 1\n", encoding="utf-8")
        git(self.workspace, "add", ".")
        git(self.workspace, "-c", "user.name=Base", "-c", "user.email=base@example.com",
            "commit", "-q", "-m", "more")
        self.base = git(self.workspace, "rev-parse", "HEAD")
        (self.workspace / "café.py").write_text("a = 2\n", encoding="utf-8")
        (self.workspace / "x b" / "y.py").write_text("a = 2\n", encoding="utf-8")
        git(self.workspace, "mv", "old.py", "new.py")

        paths = changed_paths(self.workspace, self.base)
        head = commit_candidate(
            self.workspace, base_commit=self.base, branch="mailman/issue-7",
            message="Fix the thing", identity=IDENTITY, paths=paths,
        )

        self.assertEqual(sorted(paths), ["café.py", "new.py", "old.py", "x b/y.py"])
        committed = subprocess.run(
            ["git", "-C", str(self.workspace), "diff", "--name-only", "--no-renames", "-z",
             self.base, head], check=True, capture_output=True, encoding="utf-8",
        ).stdout.split("\0")
        self.assertEqual(sorted(path for path in committed if path), sorted(paths))

    def test_a_changed_path_left_out_of_the_commit_is_refused(self) -> None:
        (self.workspace / "mod.py").write_text("x = 2\n", encoding="utf-8")
        (self.workspace / "café.py").write_text("a = 1\n", encoding="utf-8")
        git(self.workspace, "add", "--intent-to-add", "café.py")

        with self.assertRaisesRegex(ValueError, "café.py"):
            self.commit()


class SignoffTests(unittest.TestCase):
    """A target that enforces the DCO fails our pull request on its first check.
    https://github.com/wolfgang-aura/Mailman/issues/221
    """

    def workspace(self, files: dict[str, str]) -> Path:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, root, True)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        return root

    def test_a_dco_workflow_is_a_requirement(self) -> None:
        root = self.workspace({".github/workflows/dco.yml":
                               "steps:\n  - uses: tim-actions/dco@master\n"})

        self.assertEqual(signoff_requirement(root), ".github/workflows/dco.yml")

    def test_contribution_docs_that_require_a_signoff_are_a_requirement(self) -> None:
        root = self.workspace({"CONTRIBUTING.md":
                               "All commits must include a `Signed-off-by` line (DCO).\n"})

        self.assertEqual(signoff_requirement(root), "CONTRIBUTING.md")

    def test_a_readme_that_requires_git_commit_signoff_is_a_requirement(self) -> None:
        # spack states the rule only in README.md and enforces it with the DCO app (#435).
        root = self.workspace({"README.md":
                               "Your PR must:\n\n  4. Sign off all commits with `git commit "
                               "--signoff`. Signoff says that you agree to the Developer "
                               "Certificate of Origin.\n"})

        self.assertEqual(signoff_requirement(root), "README.md")

    def test_a_repository_without_either_signal_has_none(self) -> None:
        root = self.workspace({"CONTRIBUTING.md": "Run the tests before opening a PR.\n",
                               ".github/workflows/ci.yml": "steps:\n  - run: pytest\n"})

        self.assertIsNone(signoff_requirement(root))

    def test_a_message_without_the_identitys_signoff_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, r"Signed-off-by: Fixture <1\+fixture"):
            check_signoff("fix: a thing\n", IDENTITY, ".github/workflows/dco.yml")
        with self.assertRaisesRegex(ValueError, "dco.yml"):
            check_signoff("fix: a thing\n\nSigned-off-by: Someone <a@b.c>\n",
                          IDENTITY, ".github/workflows/dco.yml")

    def test_the_identitys_signoff_passes_and_no_requirement_passes(self) -> None:
        check_signoff(f"fix\n\nSigned-off-by: {IDENTITY.name} <{IDENTITY.email}>\n",
                      IDENTITY, ".github/workflows/dco.yml")
        check_signoff("fix: a thing\n", IDENTITY, None)


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

    def test_any_exception_is_a_recorded_failed_stage(self) -> None:
        """A timeout or an argparse exit escaped without a stage record. #356"""
        def times_out() -> int:
            raise subprocess.TimeoutExpired(["pytest"], 600)

        def exits() -> int:
            raise SystemExit(2)

        for stage, expected in ((times_out, "timed out"), (exits, "2")):
            with self.subTest(stage=stage.__name__):
                stream = StringIO()
                code, record = run_stages([("handoff-check", stage)], stream=stream)

                self.assertEqual(code, 2)
                self.assertEqual(record[0]["stage"], "handoff-check")
                self.assertEqual(record[0]["exit_code"], 2)
                self.assertIn(expected, stream.getvalue())


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
                head="fork:mailman/issue-7", base="main", commit_message=None, affirm=[],
            )
            (data_root / run.run_id / "decision.json").write_text("{}", encoding="utf-8")
            calls = []
            with (
                patch.object(cli, "main", side_effect=lambda argv: calls.append(argv[0]) or 0),
                patch("mailman.cli.resolve_identity", return_value=IDENTITY),
                patch("mailman.package.changed_paths", return_value=["m.py"]),
                patch("mailman.package.commit_candidate", return_value="b" * 40) as committed,
                patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()),
            ):
                code = cli._package(arguments)

        self.assertEqual(code, 0)
        self.assertEqual(calls, [
            "claims", "export-patch", "prepare-submission", "decision", "finalize-review",
            "check-authors", "handoff", "handoff-check", "review",
        ])
        self.assertEqual(committed.call_args.kwargs["branch"], "mailman/issue-7")
        self.assertEqual(committed.call_args.kwargs["paths"], ["m.py"])

    def test_an_affirmed_line_reaches_decision_and_handoff(self) -> None:
        # A template-required first-person line (#217) stopped package at the
        # decision stage: the gate read affirmations from handoff.json, which
        # only the later handoff stage writes.
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
                affirm=[5],
            )
            (data_root / run.run_id / "decision.json").write_text("{}", encoding="utf-8")
            calls = {}
            with (
                patch.object(cli, "main", side_effect=lambda argv: calls.setdefault(argv[0], argv) and 0),
                patch("mailman.cli.resolve_identity", return_value=IDENTITY),
                patch("mailman.package.changed_paths", return_value=["m.py"]),
                patch("mailman.package.commit_candidate", return_value="b" * 40),
                patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()),
            ):
                self.assertEqual(cli._package(arguments), 0)

        for stage in ("decision", "handoff"):
            argv = calls[stage]
            self.assertEqual(argv[argv.index("--affirm") + 1], "5", stage)

    def test_the_recorded_duplicate_search_is_repeated_first(self) -> None:
        # pylint, zarr and nicegui each reached handoff-check on 2026-09-30
        # with a search or claims check over an hour old. Mailman #279.
        import json

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
                head="fork:mailman/issue-7", base="main", commit_message=None, affirm=[],
            )
            directory = data_root / run.run_id
            (directory / "decision.json").write_text("{}", encoding="utf-8")
            (directory / "duplicate-search.json").write_text(json.dumps(
                {"query": "rolling window", "symbols": ["roll", "Window"]}), encoding="utf-8")
            calls = []

            def stage(argv):
                calls.append(argv)
                return 1 if argv[0] == "duplicate-search" else 0

            with (
                patch.object(cli, "main", side_effect=stage),
                patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()),
            ):
                code = cli._package(arguments)

        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:8], ["duplicate-search", run.run_id, "--query",
                                        "rolling window", "--symbol", "roll",
                                        "--symbol", "Window"])

    def test_the_repeated_search_keeps_its_limit_and_issue_symbols(self) -> None:
        # The refresh reran at the default limit without the symbols read out
        # of the issue body, so it was a narrower search than the one it
        # replaced. Mailman #355.
        import json

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
                head="fork:mailman/issue-7", base="main", commit_message=None, affirm=[],
            )
            directory = data_root / run.run_id
            (directory / "decision.json").write_text("{}", encoding="utf-8")
            (directory / "duplicate-search.json").write_text(json.dumps({
                "query": "rolling window", "symbols": [], "limit": 80,
                "issue_symbols": ["_handle_upserts"]}), encoding="utf-8")
            calls = []

            def stage(argv):
                calls.append(argv)
                return 1

            with (
                patch.object(cli, "main", side_effect=stage),
                patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()),
            ):
                cli._package(arguments)

        argv = calls[0]
        self.assertEqual(argv[argv.index("--limit") + 1], "80")
        self.assertEqual(argv[argv.index("--issue-symbol") + 1], "_handle_upserts")

    def test_a_byte_order_mark_does_not_reach_the_commit_subject(self) -> None:
        # Notepad and PowerShell 5.1 write a BOM, which then led the public
        # commit subject. Mailman #355.
        from mailman import cli

        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, _ = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40, primary="codex", reviewer="claude",
                data_root=data_root,
            )
            message_path = Path(temporary_directory) / "message.txt"
            message_path.write_text("Fix it\n", encoding="utf-8-sig")
            arguments = argparse.Namespace(
                run_id=run.run_id, data_root=data_root, policy=Path("policy.json"),
                title="Fix it", body=Path("body.md"), repo="example/project",
                head="fork:mailman/issue-7", base="main", commit_message=message_path,
                affirm=[],
            )
            (data_root / run.run_id / "decision.json").write_text("{}", encoding="utf-8")
            with (
                patch.object(cli, "main", return_value=0),
                patch("mailman.cli.resolve_identity", return_value=IDENTITY),
                patch("mailman.package.changed_paths", return_value=["m.py"]),
                patch("mailman.package.commit_candidate", return_value="b" * 40) as committed,
                patch("sys.stdout", StringIO()), patch("sys.stderr", StringIO()),
            ):
                self.assertEqual(cli._package(arguments), 0)

        self.assertEqual(committed.call_args.kwargs["message"], "Fix it\n")

    def test_an_own_words_refusal_alone_does_not_stop_packaging(self) -> None:
        # zarr run 20260930T111012Z-fcf02a stopped at prepare-submission, so
        # commit and handoff never ran and hunt status could only say REPAIR.
        # The rewrite is the operator's at filing approval. Mailman #272.
        # The handoff then withholds the publish command and handoff-check
        # refuses with own-words-pending; packaging goes on to the review
        # page and names the rewrite as the human's step. Mailman #181.
        import hashlib
        import json

        from mailman import cli

        own_words = ["policy-requires-own-words"]
        for name, codes, covers_diff, check_reason, expected in (
            ("own words alone", own_words, True, "own-words-pending", 0),
            ("beside another code", [*own_words, "missing-test"], True, "own-words-pending", 1),
            ("for an older diff", own_words, False, "own-words-pending", 1),
            ("another handoff refusal", own_words, True, "body-changed", 1),
        ):
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary_directory:
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
                    head="fork:mailman/issue-7", base="main", commit_message=None, affirm=[],
                )
                directory = data_root / run.run_id
                (directory / "decision.json").write_text("{}", encoding="utf-8")
                diff = "diff --git a/m.py b/m.py\n"
                (directory / "export").mkdir()
                (directory / "export" / "changes.diff").write_text(
                    diff, encoding="utf-8", newline="\n")
                recorded = diff if covers_diff else diff + "+older\n"
                (directory / "submission").mkdir()
                (directory / "submission" / "submission.json").write_text(
                    json.dumps({
                        "ready": False, "blocking_codes": codes,
                        "diff_sha256": hashlib.sha256(recorded.encode("utf-8")).hexdigest(),
                    }),
                    encoding="utf-8",
                )
                calls = []

                def stage(argv):
                    calls.append(argv[0])
                    return 1 if argv[0] in ("prepare-submission", "handoff-check") else 0

                printed = StringIO()
                with (
                    patch.object(cli, "main", side_effect=stage),
                    patch("mailman.cli.resolve_identity", return_value=IDENTITY),
                    patch("mailman.package.changed_paths", return_value=["m.py"]),
                    patch("mailman.package.commit_candidate", return_value="b" * 40),
                    patch("mailman.handoff.check_handoff",
                          return_value={"ok": False, "reason": check_reason}),
                    patch("sys.stdout", printed), patch("sys.stderr", StringIO()),
                ):
                    code = cli._package(arguments)

                self.assertEqual(code, expected)
                if name == "another handoff refusal":
                    self.assertIn("handoff", calls)
                    self.assertNotIn("review", calls)
                    continue
                self.assertEqual("handoff" in calls, expected == 0)
                self.assertEqual("review" in calls, expected == 0)
                self.assertEqual("own words" in printed.getvalue(), expected == 0)

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
                head="fork:mailman/issue-7", base="main", commit_message=None, affirm=[],
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


class PackageArgumentTests(unittest.TestCase):
    def test_package_without_base_is_refused_before_any_stage(self) -> None:
        # Mailman #222: package ran every stage and committed before handoff
        # refused the missing --base.
        from mailman import cli

        with patch("sys.stderr", StringIO()), \
                patch.object(cli, "_package", side_effect=AssertionError("ran")):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["package", "RUN", "--policy", "p.json", "--title", "t",
                          "--body", "b.md", "--repo", "o/r", "--head", "f:b"])
        self.assertEqual(raised.exception.code, 2)
