from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.artifacts import create_run, load_run


class ArtifactTests(unittest.TestCase):
    def test_create_run_writes_required_private_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/7",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                primary_model="codex-test-model",
                reviewer_model="claude-test-model",
                data_root=root,
            )

            self.assertTrue((run_directory / "run.json").is_file())
            self.assertTrue((run_directory / "issue.md").is_file())
            self.assertTrue((run_directory / "primary-report.md").is_file())
            self.assertTrue((run_directory / "reviewer-report.md").is_file())
            self.assertFalse((run_directory / "review-report.md").exists())
            self.assertEqual(
                json.loads((run_directory / "verification.json").read_text()), []
            )
            self.assertEqual(
                json.loads((run_directory / "toolchain.json").read_text())["tools"],
                {},
            )
            loaded, _ = load_run(run.run_id, root)
            self.assertEqual(loaded.repository, run.repository)
            self.assertEqual(loaded.primary.model, "codex-test-model")
            self.assertEqual(loaded.reviewer.model, "claude-test-model")

    def test_load_run_names_a_missing_run_instead_of_a_raw_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with self.assertRaisesRegex(
                ValueError, r"no run 'does-not-exist' under .*`mailman show`"
            ):
                load_run("does-not-exist", root)

    def test_create_run_rejects_symbolic_base_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "hexadecimal Git object ID"):
                create_run(
                    repository="https://github.com/example/project.git",
                    issue="https://github.com/example/project/issues/7",
                    base_commit="main",
                    primary="codex",
                    reviewer="claude",
                    data_root=Path(temporary_directory),
                )

    def test_create_run_rejects_abbreviated_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "full 40 or 64 character"):
                create_run(
                    repository="https://github.com/example/project.git",
                    issue="https://github.com/example/project/issues/7",
                    base_commit="abcdef0",
                    primary="codex",
                    reviewer="claude",
                    data_root=Path(temporary_directory),
                )

    def test_create_run_rejects_embedded_https_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "embedded credentials"):
                create_run(
                    repository="https://secret@example.com/project.git",
                    issue="https://github.com/example/project/issues/7",
                    base_commit="a" * 40,
                    primary="codex",
                    reviewer="claude",
                    data_root=Path(temporary_directory),
                )


if __name__ == "__main__":
    unittest.main()
