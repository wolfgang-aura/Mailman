from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from mailman.artifacts import create_run
from mailman.cli import main


class CliTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
