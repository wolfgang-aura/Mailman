from __future__ import annotations

import unittest

from mailman.orchestrator import count_candidate_files


class CandidateFileCountTests(unittest.TestCase):
    def test_tests_and_docs_do_not_count_toward_the_file_limit(self) -> None:
        # commitizen issue 1302: 3 source, 3 test and 3 docs files. Mailman #233.
        counts = count_candidate_files(
            [
                "commitizen/commands/bump.py",
                "commitizen/commands/commit.py",
                "commitizen/defaults.py",
                "docs/commands/bump.md",
                "docs/commands/commit.md",
                "docs/config/option.md",
                "tests/commands/test_bump_command.py",
                "tests/commands/test_commit_command.py",
                "tests/test_conf.py",
            ]
        )

        self.assertEqual(counts, {"source": 3, "tests": 3, "docs": 3})

    def test_classifies_common_layouts(self) -> None:
        cases = {
            "src/pkg/core.py": "source",
            "pkg/tests/test_core.py": "tests",
            "testing/test_x.py": "tests",
            "pkg/core_test.py": "tests",
            "conftest.py": "tests",
            "README.md": "docs",
            "CHANGELOG.rst": "docs",
            "changelog.d/123.bugfix": "docs",
            "news/42.fix.md": "docs",
            "changelog/7.bugfix": "docs",
            "doc/source/api.rst": "docs",
            "pyproject.toml": "source",
        }
        for path, kind in cases.items():
            with self.subTest(path=path):
                counts = count_candidate_files([path])
                self.assertEqual(counts[kind], 1, counts)


if __name__ == "__main__":
    unittest.main()
