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


if __name__ == "__main__":
    unittest.main()
