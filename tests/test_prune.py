from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from mailman.prune import BUNDLE, MARKER, WORKTREE_DIFF, plan_prune, prune_run, remove_tree

NOW = datetime(2026, 10, 22, tzinfo=UTC)
OLD = "2026-09-20T00:00:00+00:00"
RECENT = "2026-10-20T00:00:00+00:00"


def _run(root: Path, run_id: str, *, status: str = "READY_FOR_HUMAN_REVIEW",
         at: str = OLD, base: str | None = None) -> Path:
    directory = root / run_id
    (directory / "workspace").mkdir(parents=True)
    (directory / "environment").mkdir()
    (directory / "run.json").write_text(json.dumps({
        "run_id": run_id, "status": status, "created_at": at, "updated_at": at,
        "history": [], "base_commit": base, "repository": "example/project",
    }), encoding="utf-8")
    return directory


def _git(workspace: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(workspace), "-c", "user.name=t", "-c", "user.email=t@t",
                           *arguments], capture_output=True, text=True, check=True).stdout.strip()


class RemoveTreeTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "junctions are a Windows feature")
    def test_a_junction_is_removed_and_its_outside_target_survives(self) -> None:
        import _winapi

        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "outside"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep", encoding="utf-8")
            tree = Path(directory) / "tree"
            (tree / "node_modules").mkdir(parents=True)
            _winapi.CreateJunction(str(outside), str(tree / "node_modules" / "link"))
            # A loop back to the tree itself, like the openalgo runs had.
            _winapi.CreateJunction(str(tree), str(tree / "node_modules" / "self"))
            pack = tree / "pack.idx"
            pack.write_text("x", encoding="utf-8")
            os.chmod(pack, stat.S_IREAD)

            remove_tree(tree)

            self.assertFalse(tree.exists())
            self.assertEqual((outside / "keep.txt").read_text(encoding="utf-8"), "keep")


class PlanTests(unittest.TestCase):
    def test_only_an_old_run_nothing_still_uses_is_pruned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mailman = Path(directory)
            root = mailman / "runs"
            for run_id in ("old", "recent", "open-pr", "unread-pr", "merged-pr",
                           "hunted", "midway", "live"):
                _run(root, run_id, at=RECENT if run_id == "recent" else OLD,
                     status="REVIEW_PENDING" if run_id == "midway" else "READY_FOR_HUMAN_REVIEW")
            hunts = mailman / "hunts"
            for hunt_id, status, runs in (
                ("filed", "FILED", [("open-pr", 1), ("unread-pr", 2), ("merged-pr", 3)]),
                ("running", "RUNNING", [("hunted", None)]),
            ):
                (hunts / hunt_id).mkdir(parents=True)
                (hunts / hunt_id / "hunt.json").write_text(json.dumps({
                    "hunt_id": hunt_id, "status": status,
                    "runs": [{"run_id": run_id, **({"filed": {
                        "repository": "example/project", "pr_number": number}} if number else {})}
                        for run_id, number in runs],
                }), encoding="utf-8")
            (mailman / "filed-watch.json").write_text(json.dumps({"rows": [
                {"repository": "example/project", "pull_request": 1, "state": "open"},
                {"repository": "example/project", "pull_request": 3, "state": "merged"},
            ]}), encoding="utf-8")

            rows = {row["run_id"]: row for row in plan_prune(root, now=NOW, live_runs={"live"})}

            self.assertEqual(sorted(name for name, row in rows.items() if row["prune"]),
                             ["merged-pr", "old"])
            self.assertIn("is open", rows["open-pr"]["reason"])
            self.assertIn("is unknown", rows["unread-pr"]["reason"])
            self.assertEqual(rows["hunted"]["reason"], "belongs to an unfinished hunt")
            self.assertEqual(rows["midway"]["reason"], "status REVIEW_PENDING")
            self.assertIn("process", rows["live"]["reason"])


class PruneRunTests(unittest.TestCase):
    def test_commits_and_untracked_files_are_saved_before_the_clone_goes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runs"
            run = _run(root, "old")
            workspace = run / "workspace"
            _git(workspace, "init", "-q", "-b", "main")
            (workspace / "a.py").write_text("a = 1\n", encoding="utf-8")
            _git(workspace, "add", "a.py")
            _git(workspace, "commit", "-q", "-m", "base")
            base = _git(workspace, "rev-parse", "HEAD")
            _git(workspace, "checkout", "-q", "-b", "mailman/issue-1")
            (workspace / "a.py").write_text("a = 2\n", encoding="utf-8")
            _git(workspace, "commit", "-q", "-am", "fix")
            (workspace / "new_test.py").write_text("assert True\n", encoding="utf-8")
            record = json.loads((run / "run.json").read_text(encoding="utf-8"))
            (run / "run.json").write_text(json.dumps({**record, "base_commit": base}),
                                          encoding="utf-8")

            marker = prune_run(run, ["workspace", "environment"], now=NOW)

            self.assertFalse(workspace.exists())
            self.assertFalse((run / "environment").exists())
            self.assertEqual(marker["branches"], ["refs/heads/mailman/issue-1"])
            self.assertEqual(json.loads((run / MARKER).read_text(encoding="utf-8"))["bundle"], BUNDLE)
            diff = (run / WORKTREE_DIFF).read_text(encoding="utf-8")
            self.assertIn("+a = 2", diff)
            self.assertIn("new_test.py", diff)
            verified = subprocess.run(["git", "bundle", "list-heads", str(run / BUNDLE)],
                                      capture_output=True, text=True, check=True).stdout
            self.assertIn("refs/heads/mailman/issue-1", verified)

    def test_a_workspace_that_cannot_be_saved_is_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run = _run(Path(directory) / "runs", "old", base="0" * 40)
            (run / "workspace" / "edited.py").write_text("x\n", encoding="utf-8")

            with self.assertRaises(RuntimeError):
                prune_run(run, ["workspace", "environment"], now=NOW)

            self.assertTrue((run / "workspace" / "edited.py").is_file())
            self.assertFalse((run / MARKER).exists())


if __name__ == "__main__":
    unittest.main()
