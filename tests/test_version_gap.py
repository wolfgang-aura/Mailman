import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mailman.version_gap import (
    VERSION_GAP_FILENAME,
    check_version_gap,
    matching_tag,
    reported_versions,
    shared_words,
    title_words,
)

ISSUE = """# beetbox/beets#7019: beet modify uses an object string representation instead of the user-defined value when using the 'select' option

- Source: https://github.com/beetbox/beets/issues/7019

## Issue body

* Python version: 3.14.7
* beets version: 1.0.0

See beets/ui/other.py for context.

## Capture boundary
"""


def git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *arguments],
        cwd=root, check=True, capture_output=True, text=True,
    ).stdout


def commit(root: Path, path: str, subject: str) -> str:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(subject + "\n", encoding="utf-8")
    git(root, "add", path)
    git(root, "commit", "-q", "-m", subject)
    return git(root, "rev-parse", "HEAD").strip()


class VersionGapTests(unittest.TestCase):
    def test_reported_versions_reads_version_lines(self):
        self.assertEqual(reported_versions(ISSUE), ["3.14.7", "1.0.0"])

    def test_matching_tag_accepts_common_tag_shapes(self):
        self.assertEqual(matching_tag("2.13.1", ["v2.13.0", "v2.13.1"]), "v2.13.1")
        self.assertEqual(matching_tag("2.13.1", ["beets-2.13.1"]), "beets-2.13.1")
        self.assertIsNone(matching_tag("3.14.7", ["v2.13.1"]))
        self.assertIsNone(matching_tag("2.13", ["v2.13.1"]))

    def test_inflected_subject_words_match_the_title(self):
        words = title_words("beet modify uses an object string representation when using the 'select' option")
        self.assertEqual(sorted(shared_words("modify: fix selecting objects", words)), ["modify", "select"])
        self.assertEqual(shared_words("assert PositionExecutorSimulator close", ["position"]), [])

    def test_the_fix_since_the_reported_release_is_listed(self):
        """beets#7019: fixed in the next release by a commit citing another bug (#103)."""
        with tempfile.TemporaryDirectory() as name:
            run = Path(name)
            tree = run / "workspace"
            tree.mkdir()
            git(tree, "init", "-q")
            commit(tree, "beets/ui/modify.py", "initial")
            git(tree, "tag", "v1.0.0")
            fix = commit(tree, "beets/ui/modify.py", "modify: fix selecting objects")
            commit(tree, "docs/index.rst", "docs: modify the index")
            touching = commit(tree, "beets/ui/other.py", "refactor helpers")
            (run / "issue.md").write_text(ISSUE, encoding="utf-8")

            record = check_version_gap(run)

            self.assertTrue(record["success"])
            self.assertEqual(record["tag"], "v1.0.0")
            self.assertEqual(record["commits_since"], 3)
            listed = [row["commit"] for row in record["related"]]
            self.assertIn(fix, listed)
            self.assertIn(touching, listed)
            self.assertEqual(len(listed), 2)
            self.assertIn("modify: fix selecting objects", record["detail"])
            self.assertEqual(json.loads((run / VERSION_GAP_FILENAME).read_text(encoding="utf-8"))["tag"], "v1.0.0")

    def test_an_issue_reported_on_base_lists_nothing(self):
        with tempfile.TemporaryDirectory() as name:
            run = Path(name)
            tree = run / "workspace"
            tree.mkdir()
            git(tree, "init", "-q")
            commit(tree, "a.py", "initial")
            git(tree, "tag", "1.0.0")
            (run / "issue.md").write_text(ISSUE, encoding="utf-8")

            record = check_version_gap(run)

            self.assertEqual(record["commits_since"], 0)
            self.assertEqual(record["related"], [])

    def test_no_tagged_release_is_recorded_not_guessed(self):
        with tempfile.TemporaryDirectory() as name:
            run = Path(name)
            tree = run / "workspace"
            tree.mkdir()
            git(tree, "init", "-q")
            commit(tree, "a.py", "initial")
            (run / "issue.md").write_text(ISSUE, encoding="utf-8")

            record = check_version_gap(run)

            self.assertTrue(record["success"])
            self.assertIsNone(record["tag"])


if __name__ == "__main__":
    unittest.main()
