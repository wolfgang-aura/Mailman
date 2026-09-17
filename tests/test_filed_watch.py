"""The post-filing watch: every filed pull request, re-read, one line each.

edgartools#1329 failed CI on 2026-09-17 and nothing in the harness noticed;
poetry#11052 sat `behind` its base with nobody watching. These pin the four
readings that must turn the exit code red, and the record the watch leaves
behind. See https://github.com/wolfgang-aura/Mailman/issues/115.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from mailman.cli import main
from mailman.filed_watch import (
    filed_rows,
    render_watch,
    watch_filed,
    watch_path,
)
from mailman.hunt import hunt_path, save

NOW = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)
AUTHOR = "wolfgang-aura"


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Result:
    """The slice of `CommandResult` that `_Gh` reads."""

    def __init__(self, stdout: str, exit_code: int = 0, stderr: str = "",
                 timed_out: bool = False) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code, "stderr": self.stderr,
                "stdout": self.stdout, "timed_out": self.timed_out,
                "timeout_seconds": 30}


class FakeGitHub:
    """Answer `gh api` for one or more pull requests from canned payloads.

    `pulls` maps `owner/repo#N` to a dict of overrides for that pull request:
    `state`, `merged`, `mergeable_state` (a string, or a list answered one
    read at a time), `checks` (a list of check-run dicts), `statuses` (legacy
    commit statuses), `comments`, `reviews`, `review_comments`, `commits`,
    and `error` (a string that makes every call for it fail with that stderr).
    """

    def __init__(self, pulls: dict[str, dict]) -> None:
        self.pulls = pulls
        self.asked: list[str] = []
        self.mergeable_reads: dict[str, int] = {}

    def __call__(self, arguments, **keywords):
        path = arguments[-1]
        self.asked.append(path)
        base = path.split("?", 1)[0]
        parts = base.split("/")
        slug = f"{parts[1]}/{parts[2]}"
        if parts[3] == "commits":
            sha = parts[4]
            key = next(
                (key for key, pull in self.pulls.items()
                 if key.startswith(slug) and pull.get("head_sha", "head-" + key[-1]) == sha),
                None,
            )
            if key is None:
                return _Result('{"message":"Not Found"}', 1, "gh: Not Found (HTTP 404)")
            if parts[5] == "status":
                statuses = self.pulls[key].get("statuses", [])
                return _Result(json.dumps({"state": "pending", "statuses": statuses}))
            return _Result(json.dumps(
                {"total_count": len(self.pulls[key].get("checks", [])),
                 "check_runs": self.pulls[key].get("checks", [])}
            ))
        number = parts[4]
        key = f"{slug}#{number}"
        pull = self.pulls.get(key)
        if pull is None or pull.get("error"):
            detail = (pull or {}).get("error") or "gh: Not Found (HTTP 404)"
            return _Result('{"message":"Not Found"}', 1, detail)
        if len(parts) == 5:
            mergeable = pull.get("mergeable_state", "clean")
            if isinstance(mergeable, list):
                read = self.mergeable_reads.get(key, 0)
                self.mergeable_reads[key] = read + 1
                mergeable = mergeable[min(read, len(mergeable) - 1)]
            return _Result(json.dumps({
                "state": pull.get("state", "open"),
                "merged": pull.get("merged", False),
                "merged_at": _at(0.5) if pull.get("merged") else None,
                "mergeable_state": mergeable,
                "updated_at": pull.get("updated_at", _at(2)),
                "user": {"login": AUTHOR, "type": "User"},
                "head": {"sha": pull.get("head_sha", "head-" + number[-1])},
            }))
        tail = parts[5]
        if parts[3] == "issues":
            return _Result(json.dumps(pull.get("comments", [])))
        if tail == "reviews":
            return _Result(json.dumps(pull.get("reviews", [])))
        if tail == "comments":
            return _Result(json.dumps(pull.get("review_comments", [])))
        if tail == "commits":
            return _Result(json.dumps(pull.get("commits", [
                {"sha": "abc", "commit": {"committer": {"date": _at(3)},
                                          "author": {"date": _at(3)}}}
            ])))
        raise AssertionError(f"unexpected path {path}")


def _check(name: str, conclusion: str | None = "success",
           status: str = "completed") -> dict:
    return {"name": name, "status": status, "conclusion": conclusion,
            "started_at": _at(3), "completed_at": _at(3)}


def _comment(login: str, days_ago: float, *, kind: str = "User") -> dict:
    return {"user": {"login": login, "type": kind}, "created_at": _at(days_ago),
            "body": "..."}


class _Root:
    """A data root with one hunt-filed row and one provenance-only row."""

    def __init__(self, temporary: str) -> None:
        self.data_root = Path(temporary) / ".mailman" / "runs"
        self.data_root.mkdir(parents=True)

    def file_in_hunt(self, hunt_id: str, run_id: str, slug: str, number: int,
                     commit: str = "f660f7a") -> None:
        record = {
            "hunt_id": hunt_id, "status": "FILED", "requested": 1,
            "runs": [{"run_id": run_id, "filed": {
                "pr_url": f"https://github.com/{slug}/pull/{number}",
                "pr_number": number, "repository": slug,
                "target": f"{slug}#1", "commit": commit,
                "filed_at": _at(8)}}],
        }
        save(hunt_path(self.data_root, hunt_id), record)

    def file_in_provenance(self, run_id: str, slug: str, number: int,
                           **extra) -> None:
        submission = self.data_root / run_id / "submission"
        submission.mkdir(parents=True)
        (submission / "provenance.json").write_text(json.dumps({
            "schema_version": 1, "run_id": run_id, "repository": slug,
            "pull_request": number, "commits": ["339f79a"],
            "recorded_at": _at(11), **extra,
        }), encoding="utf-8")


class FiledRowsTests(unittest.TestCase):
    def test_both_ledgers_are_read_and_a_shared_row_is_one_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-poetry", "python-poetry/poetry", 11052)
            root.file_in_provenance("run-poetry", "python-poetry/poetry", 11052)
            root.file_in_provenance("run-agents", "openai/openai-agents-python", 4890)

            rows = filed_rows(root.data_root)

        self.assertEqual(
            [(row["repository"], row["pull_request"]) for row in rows],
            [("openai/openai-agents-python", 4890), ("python-poetry/poetry", 11052)],
        )
        agents, poetry = rows
        self.assertEqual(agents["sources"], ["provenance"])
        self.assertEqual(agents["run_id"], "run-agents")
        self.assertEqual(poetry["sources"], ["hunt", "provenance"])
        self.assertEqual(poetry["hunt_id"], "hunt-a")
        self.assertEqual(poetry["filed_commit"], "f660f7a")


class WatchTests(unittest.TestCase):
    def _watch(self, root: _Root, gh: FakeGitHub) -> dict:
        return watch_filed(root.data_root, executable="gh", now=NOW,
                           retry_delay_seconds=0, _execute=gh)

    def test_a_green_answered_pull_request_is_ok_and_the_record_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-1", "tqdm/tqdm", 1837)
            gh = FakeGitHub({"tqdm/tqdm#1837": {
                "checks": [_check("test"), _check("lint")],
                "comments": [_comment("maintainer", 5), _comment(AUTHOR, 4)],
            }})

            result = self._watch(root, gh)
            record = json.loads(watch_path(root.data_root).read_text(encoding="utf-8"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["needs_work"], [])
        (row,) = result["rows"]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["checks"], {"total": 2, "pending": 0, "failing": []})
        self.assertEqual(row["last_outside"]["login"], "maintainer")
        self.assertEqual(row["days_since_update"], 2)
        self.assertEqual(record["checked_at"], NOW.isoformat())
        self.assertEqual(record["rows"][0]["url"], "https://github.com/tqdm/tqdm/pull/1837")
        self.assertTrue(record["ok"])
        self.assertIn("tqdm/tqdm#1837", render_watch(result))
        self.assertIn("pass 2", render_watch(result))

    def test_a_failing_check_needs_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-1", "dgunning/edgartools", 1329)
            gh = FakeGitHub({"dgunning/edgartools#1329": {"checks": [
                _check("Tests (3.12)"),
                _check("Tests (3.13)", "failure"),
                _check("Docs", None, status="in_progress"),
            ]}})

            result = self._watch(root, gh)

        self.assertFalse(result["ok"])
        (row,) = result["rows"]
        self.assertEqual(row["status"], "attention")
        self.assertEqual(row["checks"]["failing"], ["Tests (3.13)"])
        self.assertEqual(row["checks"]["pending"], 1)
        self.assertEqual(row["reasons"], ["failing check: Tests (3.13)"])
        self.assertIn("FAIL Tests (3.13)", render_watch(result))

    def test_a_check_that_failed_and_was_rerun_green_is_not_failing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-1", "dgunning/edgartools", 1329)
            first = {**_check("Tests", "failure"), "started_at": _at(4)}
            rerun = {**_check("Tests", "success"), "started_at": _at(3)}
            gh = FakeGitHub({"dgunning/edgartools#1329": {"checks": [rerun, first]}})

            result = self._watch(root, gh)

        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"][0]["checks"], {"total": 1, "pending": 0, "failing": []})

    def test_a_failing_commit_status_counts_like_a_failing_check(self) -> None:
        """pdm reports through legacy statuses and has no check runs at all."""
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-1", "pdm-project/pdm", 3883)
            gh = FakeGitHub({"pdm-project/pdm#3883": {"statuses": [
                {"context": "ci/tests", "state": "failure", "updated_at": _at(2)},
                {"context": "ci/docs", "state": "success", "updated_at": _at(2)},
            ]}})

            result = self._watch(root, gh)

        self.assertFalse(result["ok"])
        self.assertEqual(result["rows"][0]["checks"],
                         {"total": 2, "pending": 0, "failing": ["ci/tests"]})

    def test_an_unknown_mergeable_state_is_read_a_second_time(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-1", "pdm-project/pdm", 3883)
            gh = FakeGitHub({"pdm-project/pdm#3883": {
                "mergeable_state": ["unknown", "behind"], "checks": [_check("Tests")],
            }})

            result = self._watch(root, gh)

        self.assertFalse(result["ok"])
        self.assertEqual(result["rows"][0]["mergeable_state"], "behind")
        self.assertEqual(gh.asked.count("repos/pdm-project/pdm/pulls/3883"), 2)

    def test_a_base_that_moved_on_needs_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-1", "python-poetry/poetry", 11052)
            gh = FakeGitHub({"python-poetry/poetry#11052": {
                "mergeable_state": "behind", "checks": [_check("Status")],
            }})

            result = self._watch(root, gh)

        self.assertFalse(result["ok"])
        self.assertEqual(result["rows"][0]["reasons"], ["mergeable_state behind"])

    def test_a_maintainer_comment_newer_than_our_last_commit_needs_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-1", "pdm-project/pdm", 3883)
            gh = FakeGitHub({"pdm-project/pdm#3883": {
                "checks": [_check("Tests")],
                "reviews": [{"user": {"login": "frostming", "type": "User"},
                             "submitted_at": _at(1), "state": "CHANGES_REQUESTED"}],
                "comments": [_comment("codecov[bot]", 0.5)],
                "commits": [{"sha": "abc", "commit": {
                    "committer": {"date": _at(3)}, "author": {"date": _at(3)}}}],
            }})

            result = self._watch(root, gh)

        self.assertFalse(result["ok"])
        (row,) = result["rows"]
        self.assertEqual(row["status"], "attention")
        self.assertEqual(row["last_outside"]["login"], "frostming")
        self.assertEqual(row["last_outside"]["kind"], "review")
        self.assertEqual(len(row["reasons"]), 1)
        self.assertIn("unanswered comment from frostming", row["reasons"][0])

    def test_a_maintainer_comment_we_replied_to_is_answered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-1", "pdm-project/pdm", 3883)
            gh = FakeGitHub({"pdm-project/pdm#3883": {
                "checks": [_check("Tests")],
                "comments": [_comment("frostming", 2), _comment(AUTHOR, 1)],
            }})

            result = self._watch(root, gh)

        self.assertTrue(result["ok"])

    def test_a_pull_request_gh_cannot_read_is_unknown_and_needs_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-1", "tqdm/tqdm", 1837)
            root.file_in_provenance("run-2", "ricequant/rqalpha", 1040)
            gh = FakeGitHub({
                "tqdm/tqdm#1837": {"checks": [_check("Tests")]},
                "ricequant/rqalpha#1040": {"error": "gh: API rate limit exceeded (HTTP 403)"},
            })

            result = self._watch(root, gh)
            record = json.loads(watch_path(root.data_root).read_text(encoding="utf-8"))

        self.assertFalse(result["ok"])
        by_slug = {row["repository"]: row for row in result["rows"]}
        self.assertEqual(by_slug["tqdm/tqdm"]["status"], "ok")
        unknown = by_slug["ricequant/rqalpha"]
        self.assertEqual(unknown["status"], "unknown")
        self.assertIn("rate limit", unknown["detail"])
        self.assertEqual(record["needs_work"][0]["status"], "unknown")
        self.assertIn("could not read", render_watch(result))

    def test_a_merged_or_closed_pull_request_never_needs_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_provenance("run-1", "securo-finance/securo", 875)
            root.file_in_provenance("run-2", "pdm-project/pdm", 3884, superseded_by=3890)
            gh = FakeGitHub({
                "securo-finance/securo#875": {"state": "closed", "merged": True,
                                              "checks": [_check("Tests", "failure")]},
                "pdm-project/pdm#3884": {"state": "closed", "mergeable_state": "dirty",
                                         "comments": [_comment("frostming", 0.1)]},
            })

            result = self._watch(root, gh)

        self.assertTrue(result["ok"])
        statuses = {row["repository"]: row["status"] for row in result["rows"]}
        self.assertEqual(statuses, {"securo-finance/securo": "merged",
                                    "pdm-project/pdm": "closed"})
        self.assertEqual(
            next(r for r in result["rows"] if r["pull_request"] == 3884)["superseded_by"],
            3890,
        )

    def test_the_command_exits_with_the_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            root.file_in_hunt("hunt-a", "run-1", "tqdm/tqdm", 1837)
            green = FakeGitHub({"tqdm/tqdm#1837": {"checks": [_check("Tests")]}})
            red = FakeGitHub({"tqdm/tqdm#1837": {"checks": [_check("Tests", "failure")]}})
            from unittest import mock

            codes = []
            outputs = []
            for gh in (green, red):
                out, err = StringIO(), StringIO()
                with mock.patch("mailman.filed_watch.execute", gh), \
                        mock.patch("mailman.filed_watch.resolve_tool", return_value="gh"), \
                        redirect_stdout(out), redirect_stderr(err):
                    codes.append(main(["hunt", "watch", "--data-root", str(root.data_root)]))
                outputs.append(out.getvalue())
            self.assertTrue(watch_path(root.data_root).is_file())

        self.assertEqual(codes, [0, 1])
        self.assertIn("every open pull request is green", outputs[0])
        self.assertIn("1 pull request(s) need work", outputs[1])

    def test_an_empty_ledger_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _Root(temporary)
            result = watch_filed(root.data_root, executable="gh", now=NOW,
                                 _execute=FakeGitHub({}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["rows"], [])
        self.assertIn("no filed pull requests", render_watch(result))


if __name__ == "__main__":
    unittest.main()
