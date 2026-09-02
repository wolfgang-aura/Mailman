from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mailman.artifacts import create_run
from mailman.cli import _emit, main


class CliTests(unittest.TestCase):
    def test_orchestrate_defaults_to_the_prepared_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                data_root=data_root,
            )
            workspace = run_directory / "workspace"
            workspace.mkdir()
            for name in ("primary-task.md", "reviewer-task.md"):
                (run_directory / name).write_text("prompt", encoding="utf-8")
            outcome = SimpleNamespace(
                run_id=run.run_id,
                status="READY_FOR_HUMAN_REVIEW",
                ready=True,
                revisions_used=0,
                review_cycles=1,
                record_path=run_directory / "run.json",
            )
            stderr = StringIO()
            with patch("mailman.cli.orchestrate", return_value=outcome) as orchestrated:
                with redirect_stdout(StringIO()), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "orchestrate",
                            run.run_id,
                            "--data-root",
                            str(data_root),
                            "--",
                            "true",
                        ]
                    )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertEqual(
                orchestrated.call_args.kwargs["workspace"].resolve(),
                workspace.resolve(),
            )

    def test_orchestrate_says_no_workspace_was_prepared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                data_root=data_root,
            )
            for name in ("primary-task.md", "reviewer-task.md"):
                (run_directory / name).write_text("prompt", encoding="utf-8")
            stderr = StringIO()
            with patch("mailman.cli.orchestrate") as orchestrated:
                with redirect_stdout(StringIO()), redirect_stderr(stderr):
                    exit_code = main(
                        [
                            "orchestrate",
                            run.run_id,
                            "--data-root",
                            str(data_root),
                            "--",
                            "true",
                        ]
                    )

            self.assertEqual(exit_code, 2)
            self.assertIn("no prepared workspace", stderr.getvalue())
            orchestrated.assert_not_called()

    def _verify(self, data_root: Path, run_id: str, code: str, working: str):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "verify",
                    run_id,
                    "--data-root",
                    str(data_root),
                    "--working-directory",
                    working,
                    "--timeout",
                    "30",
                    "--",
                    sys.executable,
                    "-c",
                    code,
                ]
            )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_verify_explains_a_failing_gate_on_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                data_root=data_root,
            )
            failing = (
                "import sys; print('9 failed, 57 passed'); "
                "print('assertion detail', file=sys.stderr); sys.exit(1)"
            )
            exit_code, stdout, stderr = self._verify(
                data_root, run.run_id, failing, temporary_directory
            )

            self.assertEqual(exit_code, 1)
            summary = json.loads(stdout)
            self.assertEqual(summary["exit_code"], 1)
            self.assertIn("verification exited 1", stderr)
            self.assertIn("9 failed, 57 passed", stderr)
            self.assertIn("assertion detail", stderr)
            record = run_directory / "commands" / f"{summary['record']:04d}.json"
            self.assertIn(str(record), stderr)

            passing = "print('all good')"
            exit_code, stdout, stderr = self._verify(
                data_root, run.run_id, passing, temporary_directory
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(stdout)["exit_code"], 0)
            self.assertEqual(stderr, "")

    def _run_for_prior_art(self, data_root: Path):
        return create_run(
            repository="https://github.com/example/project.git",
            issue="https://github.com/example/project/issues/7",
            base_commit="a" * 40,
            primary="codex",
            reviewer="claude",
            data_root=data_root,
        )

    def test_prior_art_records_an_empty_result_when_nothing_was_tried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, run_directory = self._run_for_prior_art(data_root)
            (run_directory / "duplicate-search.json").write_text(
                json.dumps({"schema_version": 1, "success": True, "matches": []}),
                encoding="utf-8",
            )
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "prior-art",
                        run.run_id,
                        "--executable",
                        "gh-not-called",
                        "--data-root",
                        str(data_root),
                    ]
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            self.assertEqual(json.loads(stdout.getvalue())["attempts"], 0)
            record = json.loads(
                (run_directory / "prior-art.json").read_text(encoding="utf-8")
            )
            self.assertTrue(record["success"])
            self.assertEqual(record["attempts"], [])
            self.assertIn(
                "No earlier pull request was found",
                (run_directory / "prior-art.md").read_text(encoding="utf-8"),
            )

    def test_acknowledge_duplicates_pins_the_rows_it_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, run_directory = self._run_for_prior_art(data_root)
            (run_directory / "duplicate-search.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "success": True,
                        "complete": True,
                        "matches": [
                            {
                                "number": 1386,
                                "title": "ENH: Add exit tags",
                                "state": "OPEN",
                                "pull_request": True,
                                "matched_by": ["price"],
                                "methods": ["listing"],
                                "matched_terms": ["price"],
                                "term_count": 4,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            stdout = StringIO()
            with redirect_stdout(stdout), redirect_stderr(StringIO()):
                exit_code = main(
                    [
                        "acknowledge-duplicates",
                        run.run_id,
                        "--note",
                        "read it, it adds exit tags",
                        "--data-root",
                        str(data_root),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(json.loads(stdout.getvalue())["reviewed"], ["pr#1386"])
            record = json.loads(
                (run_directory / "duplicate-acknowledgement.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(record["note"], "read it, it adds exit tags")

    def test_acknowledge_duplicates_refuses_without_a_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, _ = self._run_for_prior_art(data_root)
            stderr = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "acknowledge-duplicates",
                        run.run_id,
                        "--note",
                        "nothing to read",
                        "--data-root",
                        str(data_root),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("no duplicate search to acknowledge", stderr.getvalue())

    def test_prior_art_still_refuses_when_no_search_was_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, _ = self._run_for_prior_art(data_root)
            stderr = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                exit_code = main(
                    ["prior-art", run.run_id, "--data-root", str(data_root)]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("no duplicate search recorded", stderr.getvalue())

    def test_prior_art_refuses_a_duplicate_search_that_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, run_directory = self._run_for_prior_art(data_root)
            (run_directory / "duplicate-search.json").write_text(
                json.dumps({"schema_version": 1, "success": False, "matches": []}),
                encoding="utf-8",
            )
            stderr = StringIO()
            with redirect_stdout(StringIO()), redirect_stderr(stderr):
                exit_code = main(
                    ["prior-art", run.run_id, "--data-root", str(data_root)]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("did not succeed", stderr.getvalue())

    def test_show_names_a_missing_run_instead_of_printing_an_errno(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            data_root.mkdir()
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    ["show", "does-not-exist", "--data-root", str(data_root)]
                )

            self.assertEqual(exit_code, 2)
            message = stderr.getvalue()
            self.assertIn("no run 'does-not-exist'", message)
            self.assertIn("mailman show", message)
            self.assertNotIn("run.json", message)

    def test_init_run_refuses_an_agent_name_no_adapter_can_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "init-run",
                        "--repository",
                        "https://github.com/example/project.git",
                        "--issue",
                        "https://github.com/example/project/issues/7",
                        "--base-commit",
                        "a" * 40,
                        "--primary",
                        "codx",
                        "--reviewer",
                        "claude",
                        "--primary-model",
                        "codex-test-model",
                        "--reviewer-model",
                        "claude-test-model",
                        "--data-root",
                        str(data_root),
                    ]
                )

            self.assertEqual(exit_code, 2)
            self.assertIn("unsupported engineering agent", stderr.getvalue())
            self.assertFalse(list(data_root.glob("*")) if data_root.is_dir() else [])

    def test_verify_accepts_mailman_options_after_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                data_root=data_root,
            )
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "verify",
                        run.run_id,
                        "--data-root",
                        str(data_root),
                        "--working-directory",
                        temporary_directory,
                        "--timeout",
                        "5",
                        "--",
                        sys.executable,
                        "-c",
                        "print('ok')",
                    ]
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            records = json.loads(
                (run_directory / "verification.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["stdout"].strip(), "ok")

    def test_build_prompts_expands_the_environment_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                data_root=data_root,
            )
            (run_directory / "issue.md").write_text("# Issue\n\nBody.\n", encoding="utf-8")
            stdout = StringIO()
            stderr = StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(
                    [
                        "build-prompts",
                        run.run_id,
                        "--verification",
                        "{environment}/bin/python -m pytest",
                        "--data-root",
                        str(data_root),
                    ]
                )

            self.assertEqual(exit_code, 0, stderr.getvalue())
            prompt = (run_directory / "primary-task.md").read_text(encoding="utf-8")
            # An agent reading a literal `{environment}` cannot run anything.
            self.assertNotIn("{environment}", prompt)
            self.assertIn("environment/bin/python -m pytest", prompt.replace("\\", "/"))

    def test_show_renders_a_run_and_lists_them_without_a_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                data_root=data_root,
            )
            (run_directory / "agent-executions").mkdir(parents=True, exist_ok=True)
            (run_directory / "agent-executions" / "0001-primary.json").write_text(
                json.dumps(
                    {
                        "agent": "codex",
                        "role": "primary",
                        "process": {
                            "stdout": json.dumps(
                                {
                                    "type": "item.completed",
                                    "item": {
                                        "type": "agent_message",
                                        "text": "Reproduced the failure.",
                                    },
                                }
                            )
                        },
                    }
                ),
                encoding="utf-8",
            )

            listing = StringIO()
            with redirect_stdout(listing):
                listed = main(["show", "--data-root", str(data_root)])
            detail = StringIO()
            with redirect_stdout(detail):
                shown = main(["show", run.run_id, "--data-root", str(data_root)])

        self.assertEqual(listed, 0)
        self.assertIn(run.run_id, listing.getvalue())
        self.assertEqual(shown, 0)
        self.assertIn("Reproduced the failure.", detail.getvalue())

    def test_emit_survives_a_console_that_cannot_encode_the_transcript(self) -> None:
        class LegacyConsole:
            encoding = "cp437"

            def __init__(self) -> None:
                self.buffer = io.BytesIO()

            def write(self, text: str) -> int:
                raise UnicodeEncodeError("cp437", text, 0, 1, "not encodable")

            def flush(self) -> None:
                pass

        console = LegacyConsole()
        with redirect_stdout(console):
            _emit("the agent said — done")

        self.assertIn(b"done", console.buffer.getvalue())

    def test_show_rejects_a_run_id_that_escapes_the_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            data_root.mkdir(parents=True)
            stderr = StringIO()
            with redirect_stderr(stderr):
                exit_code = main(
                    ["show", "../secrets", "--data-root", str(data_root)]
                )

        self.assertEqual(exit_code, 2)
        self.assertIn("invalid run ID", stderr.getvalue())


class InitRunModelTests(unittest.TestCase):
    def test_init_run_refuses_to_pick_a_model_for_the_operator(self) -> None:
        """A silent vendor default is a run that cannot say what ran it."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            stderr = StringIO()
            with self.assertRaises(SystemExit), redirect_stderr(stderr):
                main(
                    [
                        "init-run",
                        "--repository",
                        "https://github.com/example/project.git",
                        "--issue",
                        "https://github.com/example/project/issues/7",
                        "--base-commit",
                        "a" * 40,
                        "--primary",
                        "codex",
                        "--reviewer",
                        "claude",
                        "--data-root",
                        str(data_root),
                    ]
                )

            self.assertIn("--primary-model", stderr.getvalue())
            self.assertIn("--reviewer-model", stderr.getvalue())

    def test_init_run_records_both_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "init-run",
                        "--repository",
                        "https://github.com/example/project.git",
                        "--issue",
                        "https://github.com/example/project/issues/7",
                        "--base-commit",
                        "a" * 40,
                        "--primary",
                        "codex",
                        "--reviewer",
                        "claude",
                        "--primary-model",
                        "codex-model-id",
                        "--reviewer-model",
                        "claude-model-id",
                        "--data-root",
                        str(data_root),
                    ]
                )

            self.assertEqual(exit_code, 0)
            run_id = json.loads(stdout.getvalue())["run_id"]
            record = json.loads(
                (data_root / run_id / "run.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["primary"]["model"], "codex-model-id")
            self.assertEqual(record["reviewer"]["model"], "claude-model-id")


class StreamFlushTests(unittest.TestCase):
    def test_a_streamed_line_arrives_before_the_process_exits(self) -> None:
        """A redirected run must not sit silent until its buffer fills.

        Python line-buffers a terminal and block-buffers everything else, so
        without an explicit flush a piped `orchestrate` says nothing for the
        first 8 KB, which is the black box decision 0008 removed.
        """
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from mailman.cli import _emit; _emit('streamed line'); "
                "import sys; sys.stdin.readline()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.addCleanup(child.kill)
        # Without the flush the read below never returns, so the child is put
        # out of its misery rather than hanging the suite.
        watchdog = threading.Timer(20, child.kill)
        watchdog.start()
        self.addCleanup(watchdog.cancel)
        assert child.stdout is not None
        line = child.stdout.readline()
        self.assertEqual(line.strip(), "streamed line")


if __name__ == "__main__":
    unittest.main()
