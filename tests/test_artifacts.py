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
                data_root=root,
            )

            self.assertTrue((run_directory / "run.json").is_file())
            self.assertTrue((run_directory / "issue.md").is_file())
            self.assertTrue((run_directory / "primary-report.md").is_file())
            self.assertTrue((run_directory / "review-report.md").is_file())
            self.assertEqual(
                json.loads((run_directory / "verification.json").read_text()), []
            )
            loaded, _ = load_run(run.run_id, root)
            self.assertEqual(loaded.repository, run.repository)

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


if __name__ == "__main__":
    unittest.main()
