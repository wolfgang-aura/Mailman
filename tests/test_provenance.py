"""Evidence a contribution survives the fork that carried it."""

from __future__ import annotations

import json
import subprocess
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from mailman.provenance import (
    ProvenanceError,
    collect_contributions,
    competitors,
    competitors_from_timeline,
    contribution_from_record,
    deletion_is_safe,
    load_provenance,
    record_provenance,
    refresh_contributions,
    refresh_state,
    render_contributions,
    repository_slug,
    state_is_stale,
    unrecorded_submissions,
    upstream_issue_number,
    write_patch,
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


def _repository(root: Path) -> tuple[Path, str]:
    path = root / "workspace"
    path.mkdir(parents=True)
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.name", "wolfgang-aura")
    _git(path, "config", "user.email", "9+w@users.noreply.github.com")
    (path / "core.py").write_text("value = 1\n", encoding="utf-8")
    _git(path, "add", "core.py")
    _git(path, "commit", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    (path / "core.py").write_text("value = 2\n", encoding="utf-8")
    _git(path, "commit", "-am", "Align the value")
    return path, base


def _merged(repository: str, number: int) -> dict[str, object]:
    return {
        "available": True,
        "state": "MERGED",
        "merged_at": "2026-09-06T00:00:00Z",
        "merge_commit": "b" * 40,
        "url": f"https://github.com/{repository}/pull/{number}",
        "title": "Align the value",
    }


def _open(repository: str, number: int) -> dict[str, object]:
    return {
        "available": True,
        "state": "OPEN",
        "merged_at": None,
        "merge_commit": None,
        "url": f"https://github.com/{repository}/pull/{number}",
        "title": "Align the value",
    }


def _closed(repository: str, number: int) -> dict[str, object]:
    return {
        "available": True,
        "state": "CLOSED",
        "merged_at": None,
        "merge_commit": None,
        "url": f"https://github.com/{repository}/pull/{number}",
        "title": "Align the value",
    }


def _offline(repository: str, number: int) -> dict[str, object]:
    return {"available": False, "detail": "gh is not installed"}


class RepositorySlugTests(unittest.TestCase):
    def test_a_clone_url_becomes_a_slug(self) -> None:
        self.assertEqual(
            repository_slug("https://github.com/pmorissette/ffn.git"), "pmorissette/ffn"
        )

    def test_an_ssh_url_becomes_a_slug(self) -> None:
        self.assertEqual(
            repository_slug("git@github.com:pmorissette/ffn.git"), "pmorissette/ffn"
        )

    def test_a_slug_survives_unchanged(self) -> None:
        self.assertEqual(repository_slug("pmorissette/ffn"), "pmorissette/ffn")

    def test_nonsense_is_refused(self) -> None:
        with self.assertRaises(ProvenanceError):
            repository_slug("not a repository")


class PatchTests(unittest.TestCase):
    def test_the_patch_keeps_the_author_and_the_message(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            workspace, base = _repository(root)
            destination = write_patch(workspace, base, root / "out.patch")
            text = destination.read_text(encoding="utf-8")
            self.assertIn("Align the value", text)
            self.assertIn("wolfgang-aura", text)
            self.assertIn("-value = 1", text)
            self.assertIn("+value = 2", text)

    def test_a_branch_with_no_commits_is_refused(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            workspace, _ = _repository(root)
            head = _git(workspace, "rev-parse", "HEAD")
            with self.assertRaises(ProvenanceError):
                write_patch(workspace, head, root / "out.patch")


class HeadTipTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/84

    python/mypy#21961 was squashed and force-pushed after a reviewer asked, and
    the first provenance call wrote permalinks to two commits that no longer
    existed on any branch.
    """

    def test_a_workspace_matching_the_branch_tip_is_recorded(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            workspace, base = _repository(root)
            tip = _git(workspace, "rev-parse", "HEAD")

            record = record_provenance(
                run_id="20260909T000000Z-aaaaaa",
                run_directory=root,
                repository="python/mypy",
                base_commit=base,
                workspace=workspace,
                pull_request=21961,
                head="wolfgang-aura:mailman/issue-1",
                state_lookup=_merged,
                head_lookup=lambda repository, head: tip,
            )

            self.assertEqual(record["head"], "wolfgang-aura:mailman/issue-1")
            self.assertEqual(record["commits"], [tip])

    def test_a_force_pushed_branch_refuses_rather_than_writing_dead_links(
        self,
    ) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            workspace, base = _repository(root)

            with self.assertRaisesRegex(ProvenanceError, "force-pushed"):
                record_provenance(
                    run_id="20260909T000000Z-bbbbbb",
                    run_directory=root,
                    repository="python/mypy",
                    base_commit=base,
                    workspace=workspace,
                    pull_request=21961,
                    head="wolfgang-aura:mailman/issue-1",
                    state_lookup=_merged,
                    head_lookup=lambda repository, head: "f" * 40,
                )
            self.assertIsNone(load_provenance(root))

    def test_an_unreadable_branch_is_refused_not_assumed_to_agree(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            workspace, base = _repository(root)

            with self.assertRaisesRegex(ProvenanceError, "could not read the tip"):
                record_provenance(
                    run_id="20260909T000000Z-cccccc",
                    run_directory=root,
                    repository="python/mypy",
                    base_commit=base,
                    workspace=workspace,
                    head="wolfgang-aura:mailman/issue-1",
                    state_lookup=_merged,
                    head_lookup=lambda repository, head: None,
                )

    def test_a_recorded_head_is_rechecked_on_a_later_pass(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            workspace, base = _repository(root)
            tip = _git(workspace, "rev-parse", "HEAD")
            record_provenance(
                run_id="20260909T000000Z-dddddd",
                run_directory=root,
                repository="python/mypy",
                base_commit=base,
                workspace=workspace,
                head="wolfgang-aura:mailman/issue-1",
                state_lookup=_merged,
                head_lookup=lambda repository, head: tip,
            )

            with self.assertRaisesRegex(ProvenanceError, "force-pushed"):
                record_provenance(
                    run_id="20260909T000000Z-dddddd",
                    run_directory=root,
                    repository="python/mypy",
                    base_commit=base,
                    workspace=workspace,
                    state_lookup=_merged,
                    head_lookup=lambda repository, head: "f" * 40,
                )


class UnrecordedSubmissionTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/84

    Two PRs filed on 2026-09-09 were absent from the ledger for a day, and the
    ledger read as complete throughout.
    """

    def _ready_run(self, data_root: Path, run_id: str) -> Path:
        directory = data_root / run_id
        (directory / "submission").mkdir(parents=True)
        (directory / "submission" / "submission.json").write_text(
            json.dumps({"ready": True}), encoding="utf-8"
        )
        return directory

    def test_a_ready_submission_without_provenance_is_listed(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            self._ready_run(data_root, "20260908T220821Z-124828")

            self.assertEqual(
                unrecorded_submissions(data_root), ["20260908T220821Z-124828"]
            )

    def test_a_recorded_run_is_not_listed(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            directory = self._ready_run(data_root, "20260908T220821Z-124828")
            (directory / "submission" / "provenance.json").write_text(
                json.dumps({"run_id": "20260908T220821Z-124828"}), encoding="utf-8"
            )

            self.assertEqual(unrecorded_submissions(data_root), [])

    def test_an_unready_submission_is_not_listed(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            directory = data_root / "20260908T220821Z-124828" / "submission"
            directory.mkdir(parents=True)
            (directory / "submission.json").write_text(
                json.dumps({"ready": False}), encoding="utf-8"
            )

            self.assertEqual(unrecorded_submissions(data_root), [])


class RecordTests(unittest.TestCase):
    def test_a_merged_pull_request_is_recorded_with_its_merge_commit(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            workspace, base = _repository(root)
            record = record_provenance(
                run_id="20260906T000000Z-cccccc",
                run_directory=root,
                repository="https://github.com/pmorissette/ffn.git",
                base_commit=base,
                workspace=workspace,
                pull_request=330,
                state_lookup=_merged,
            )
            self.assertEqual(record["repository"], "pmorissette/ffn")
            self.assertEqual(record["state"], "MERGED")
            self.assertEqual(record["merge_commit"], "b" * 40)
            self.assertEqual(len(record["commits"]), 1)
            self.assertTrue(
                record["permalinks"][0].startswith(
                    "https://github.com/pmorissette/ffn/commit/"
                )
            )
            self.assertTrue(Path(record["patch_path"]).is_file())
            self.assertEqual(load_provenance(root), record)

    def test_a_superseded_pull_request_names_the_one_that_carried_it(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            workspace, base = _repository(root)
            record = record_provenance(
                run_id="20260906T000000Z-dddddd",
                run_directory=root,
                repository="pmorissette/ffn",
                base_commit=base,
                workspace=workspace,
                pull_request=328,
                superseded_by=330,
                state_lookup=_closed,
            )
            self.assertEqual(record["state"], "CLOSED")
            self.assertEqual(record["superseded_by"], 330)

    def test_a_second_pass_keeps_what_the_first_established(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            workspace, base = _repository(root)
            record_provenance(
                run_id="20260906T000000Z-eeeeee",
                run_directory=root,
                repository="pmorissette/ffn",
                base_commit=base,
                workspace=workspace,
                pull_request=328,
                superseded_by=330,
                state_lookup=_closed,
            )
            second = record_provenance(
                run_id="20260906T000000Z-eeeeee",
                run_directory=root,
                repository="pmorissette/ffn",
                base_commit=base,
                workspace=workspace,
                state_lookup=_closed,
            )
            self.assertEqual(second["pull_request"], 328)
            self.assertEqual(second["superseded_by"], 330)

    def test_an_unreachable_github_leaves_the_state_unclaimed(self) -> None:
        with TemporaryDirectory() as name:
            root = Path(name)
            workspace, base = _repository(root)
            record = record_provenance(
                run_id="20260906T000000Z-ffffff",
                run_directory=root,
                repository="pmorissette/ffn",
                base_commit=base,
                workspace=workspace,
                pull_request=330,
                state_lookup=_offline,
            )
            self.assertIsNone(record["state"])
            self.assertFalse(record["lookup"]["available"])


class DeletionTests(unittest.TestCase):
    def test_a_merged_pull_request_frees_the_fork(self) -> None:
        safe, reason = deletion_is_safe({"state": "MERGED"})
        self.assertTrue(safe)
        self.assertIn("upstream", reason)

    def test_an_open_pull_request_holds_the_fork(self) -> None:
        safe, reason = deletion_is_safe({"state": "OPEN"})
        self.assertFalse(safe)
        self.assertIn("would close it", reason)

    def test_a_patch_on_disk_frees_a_closed_pull_request(self) -> None:
        with TemporaryDirectory() as name:
            patch = Path(name) / "contribution.patch"
            patch.write_text("From abc\n", encoding="utf-8")
            safe, reason = deletion_is_safe(
                {"state": "CLOSED", "patch_path": str(patch)}
            )
            self.assertTrue(safe)
            self.assertIn("patch", reason)

    def test_a_closed_pull_request_with_no_patch_is_not_safe(self) -> None:
        safe, reason = deletion_is_safe({"state": "CLOSED"})
        self.assertFalse(safe)
        self.assertIn("nothing proves", reason)

    def test_a_recorded_patch_that_is_gone_is_not_safe(self) -> None:
        safe, _ = deletion_is_safe(
            {"state": "CLOSED", "patch_path": "/nowhere/contribution.patch"}
        )
        self.assertFalse(safe)


class ListingTests(unittest.TestCase):
    def test_runs_without_provenance_are_skipped(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            (data_root / "20260101T000000Z-aaaaaa").mkdir()
            self.assertEqual(collect_contributions(data_root), [])
            self.assertIn("no run", render_contributions([]))

    def test_a_recorded_run_is_listed_with_its_permalink(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            run_directory = data_root / "20260906T000000Z-cccccc"
            workspace_root = run_directory
            workspace, base = _repository(workspace_root)
            record_provenance(
                run_id="20260906T000000Z-cccccc",
                run_directory=run_directory,
                repository="pmorissette/ffn",
                base_commit=base,
                workspace=workspace,
                pull_request=330,
                state_lookup=_merged,
            )
            found = collect_contributions(data_root)
            self.assertEqual(len(found), 1)
            rendered = render_contributions(found)
            self.assertIn("pmorissette/ffn", rendered)
            self.assertIn("#330", rendered)
            self.assertIn("MERGED", rendered)
            self.assertIn("/commit/", rendered)
            self.assertIn("merged as", rendered)

    def test_a_record_round_trips_through_the_dataclass(self) -> None:
        record = {
            "run_id": "r",
            "repository": "pmorissette/ffn",
            "commits": ["a" * 40],
            "pull_request": 328,
            "state": "CLOSED",
            "superseded_by": 330,
        }
        contribution = contribution_from_record(record)
        payload = contribution.to_dict()
        self.assertEqual(payload["superseded_by"], 330)
        self.assertEqual(
            payload["permalinks"],
            ["https://github.com/pmorissette/ffn/commit/" + "a" * 40],
        )
        self.assertEqual(json.loads(json.dumps(payload))["state"], "CLOSED")


# The cross-references on python/mypy#21960, as `gh api .../timeline --paginate
# --slurp` returned them on 2026-09-11, trimmed to the fields read. Ours is
# #21961; #21967 is the competing fix; the last is this tracker's own issue
# #85, which is neither a pull request nor in the target repository.
# https://github.com/wolfgang-aura/Mailman/issues/86
MYPY_21960_TIMELINE: list[dict[str, object]] = [
    {"event": "labeled"},
    {
        "event": "cross-referenced",
        "source": {
            "type": "issue",
            "issue": {
                "number": 21961,
                "state": "closed",
                "html_url": "https://github.com/python/mypy/pull/21961",
                "repository_url": "https://api.github.com/repos/python/mypy",
                "created_at": "2026-09-09T02:12:50Z",
                "user": {"login": "wolfgang-aura"},
                "pull_request": {
                    "url": "https://api.github.com/repos/python/mypy/pulls/21961",
                    "merged_at": None,
                },
            },
        },
    },
    {"event": "subscribed"},
    {"event": "referenced"},
    {
        "event": "cross-referenced",
        "source": {
            "type": "issue",
            "issue": {
                "number": 21967,
                "state": "open",
                "html_url": "https://github.com/python/mypy/pull/21967",
                "repository_url": "https://api.github.com/repos/python/mypy",
                "created_at": "2026-09-10T20:17:05Z",
                "user": {"login": "EmmanuelNiyonshuti"},
                "pull_request": {
                    "url": "https://api.github.com/repos/python/mypy/pulls/21967",
                    "merged_at": None,
                },
            },
        },
    },
    {
        "event": "cross-referenced",
        "source": {
            "type": "issue",
            "issue": {
                "number": 85,
                "state": "open",
                "html_url": "https://github.com/wolfgang-aura/Mailman/issues/85",
                "repository_url": "https://api.github.com/repos/wolfgang-aura/Mailman",
                "created_at": "2026-09-10T20:21:38Z",
                "user": {"login": "wolfgang-aura"},
                "pull_request": None,
            },
        },
    },
]


def _mypy_competitors(repository: str, issue: int, *, own_number: int) -> dict[str, object]:
    return {
        "available": True,
        # The recorded timeline is mypy's whatever repository the test filed against.
        "pull_requests": competitors_from_timeline(
            MYPY_21960_TIMELINE, "python/mypy", own_number=own_number
        ),
    }


def _no_competitors(repository: str, issue: int, *, own_number: int) -> dict[str, object]:
    return {"available": True, "pull_requests": []}


def _competitors_offline(
    repository: str, issue: int, *, own_number: int
) -> dict[str, object]:
    return {"available": False, "detail": "gh is not installed"}


def _name_the_issue(run_directory: Path, issue: str) -> None:
    (run_directory / "run.json").write_text(
        json.dumps({"run_id": run_directory.name, "issue": issue}), encoding="utf-8"
    )


class CompetingPullRequestTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/86."""

    def test_the_timeline_yields_the_other_pull_request_and_nothing_else(self) -> None:
        found = competitors_from_timeline(
            MYPY_21960_TIMELINE, "python/mypy", own_number=21961
        )

        self.assertEqual(
            found,
            [
                {
                    "number": 21967,
                    "state": "open",
                    "author": "EmmanuelNiyonshuti",
                    "url": "https://github.com/python/mypy/pull/21967",
                    "created_at": "2026-09-10T20:17:05Z",
                }
            ],
        )

    def test_a_merged_competitor_is_reported_as_merged(self) -> None:
        events = [
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 7,
                        "state": "closed",
                        "html_url": "https://github.com/pdm-project/pdm/pull/7",
                        "repository_url": "https://api.github.com/repos/pdm-project/pdm",
                        "created_at": "2026-09-01T00:00:00Z",
                        "user": {"login": "other"},
                        "pull_request": {"merged_at": "2026-09-02T00:00:00Z"},
                    }
                },
            },
            {
                "event": "cross-referenced",
                "source": {
                    "issue": {
                        "number": 8,
                        "state": "closed",
                        "html_url": "https://github.com/pdm-project/pdm/pull/8",
                        "repository_url": "https://api.github.com/repos/pdm-project/pdm",
                        "created_at": "2026-09-01T00:00:00Z",
                        "user": {"login": "other"},
                        "pull_request": {"merged_at": None},
                    }
                },
            },
        ]

        found = competitors_from_timeline(events, "pdm-project/pdm", own_number=9)

        self.assertEqual([item["number"] for item in found], [7])
        self.assertEqual(found[0]["state"], "merged")

    def test_the_issue_number_comes_from_the_run_record(self) -> None:
        with TemporaryDirectory() as name:
            run_directory = Path(name)
            self.assertIsNone(upstream_issue_number(run_directory, "python/mypy"))

            _name_the_issue(run_directory, "https://github.com/python/mypy/issues/21960")
            self.assertEqual(upstream_issue_number(run_directory, "python/mypy"), 21960)
            self.assertEqual(
                upstream_issue_number(
                    run_directory, "https://github.com/python/mypy.git"
                ),
                21960,
            )
            # An issue in another repository says nothing about this one's competitors.
            self.assertIsNone(upstream_issue_number(run_directory, "pdm-project/pdm"))

    def test_the_staged_submission_is_the_second_source(self) -> None:
        with TemporaryDirectory() as name:
            run_directory = Path(name)
            submission = run_directory / "submission"
            submission.mkdir()
            (submission / "submission.json").write_text(
                json.dumps({"target": "python/mypy", "issue_number": 21964}),
                encoding="utf-8",
            )

            self.assertEqual(upstream_issue_number(run_directory, "python/mypy"), 21964)
            self.assertIsNone(upstream_issue_number(run_directory, "pdm-project/pdm"))

    def test_a_refresh_records_the_competitor_on_an_open_pull_request(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            run_directory = _filed_run(data_root, "20260908T204404Z-1e0aa3", 21961)
            _name_the_issue(run_directory, "https://github.com/pdm-project/pdm/issues/3")
            now = datetime(2026, 9, 11, 6, 0, tzinfo=UTC)

            record, failure = refresh_state(
                run_directory,
                state_lookup=_open,
                competitor_lookup=_mypy_competitors,
                now=now,
            )

            self.assertIsNone(failure)
            self.assertEqual(record["competition"]["issue"], 3)
            self.assertEqual(record["competition"]["checked_at"], now.isoformat())
            entry = contribution_from_record(load_provenance(run_directory))
            self.assertEqual([item["number"] for item in competitors(entry)], [21967])

    def test_a_closed_pull_request_is_not_looked_up(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            run_directory = _filed_run(data_root, "20260908T204404Z-1e0aa3", 21961)
            _name_the_issue(run_directory, "https://github.com/pdm-project/pdm/issues/3")

            def _never(repository: str, issue: int, *, own_number: int) -> dict[str, object]:
                raise AssertionError("a closed pull request cannot be overtaken")

            record, failure = refresh_state(
                run_directory, state_lookup=_closed, competitor_lookup=_never
            )

            self.assertIsNone(failure)
            self.assertIn("CLOSED", record["competition"]["skipped"])
            self.assertEqual(competitors(contribution_from_record(record)), [])

    def test_an_unreadable_issue_is_a_failure_not_a_clean_bill(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            run_directory = _filed_run(data_root, "20260908T204404Z-1e0aa3", 21961)
            _name_the_issue(run_directory, "https://github.com/pdm-project/pdm/issues/3")

            record, failure = refresh_state(
                run_directory, state_lookup=_open, competitor_lookup=_competitors_offline
            )

            self.assertIn("competitors unchecked", failure)
            self.assertIn("gh is not installed", failure)
            self.assertEqual(record["state"], "OPEN")
            self.assertEqual(competitors(contribution_from_record(record)), [])

    def test_a_run_that_names_no_issue_says_so(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            run_directory = _filed_run(data_root, "20260908T204404Z-1e0aa3", 21961)

            record, failure = refresh_state(
                run_directory, state_lookup=_open, competitor_lookup=_mypy_competitors
            )

            self.assertIn("names no issue", failure)
            rendered = render_contributions([contribution_from_record(record)])
            self.assertIn("competing pull requests unchecked", rendered)

    def test_the_listing_names_the_competitor_and_the_all_clear(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            challenged = _filed_run(data_root, "20260908T204404Z-1e0aa3", 21961)
            _name_the_issue(challenged, "https://github.com/pdm-project/pdm/issues/3")
            clear = _filed_run(data_root, "20260908T223126Z-2b2b81", 14993)
            _name_the_issue(clear, "https://github.com/pdm-project/pdm/issues/4")
            refresh_state(challenged, state_lookup=_open, competitor_lookup=_mypy_competitors)
            refresh_state(clear, state_lookup=_open, competitor_lookup=_no_competitors)

            rendered = render_contributions(collect_contributions(data_root))

            self.assertIn(
                "COMPETING: #21967 open by EmmanuelNiyonshuti, opened "
                "2026-09-10T20:17:05Z https://github.com/python/mypy/pull/21967",
                rendered,
            )
            self.assertIn("no competing pull request on issue #4", rendered)

    def test_an_open_pull_request_never_checked_says_so(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            _filed_run(data_root, "20260908T204404Z-1e0aa3", 21961)

            rendered = render_contributions(collect_contributions(data_root))

            self.assertIn("competing pull requests never read", rendered)


if __name__ == "__main__":
    unittest.main()


def _unavailable(repository: str, number: int) -> dict[str, object]:
    return {"available": False, "detail": "gh is not installed"}


def _filed_run(data_root: Path, run_id: str, number: int) -> Path:
    """A run that filed a pull request and has not re-read it since."""
    run_directory = data_root / run_id
    workspace, base = _repository(run_directory)
    record_provenance(
        run_id=run_id,
        run_directory=run_directory,
        repository="pdm-project/pdm",
        base_commit=base,
        workspace=workspace,
        pull_request=number,
        state_lookup=_open,
    )
    return run_directory


class RefreshTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/78."""

    def test_a_closed_pull_request_stops_reading_open(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            run_directory = _filed_run(data_root, "20260907T173348Z-003915", 3884)
            self.assertEqual(load_provenance(run_directory)["state"], "OPEN")

            now = datetime(2026, 9, 9, 8, 44, tzinfo=UTC)
            record, failure = refresh_state(
                run_directory, state_lookup=_closed, now=now
            )

            self.assertIsNone(failure)
            self.assertEqual(record["state"], "CLOSED")
            self.assertEqual(record["checked_at"], now.isoformat())
            self.assertEqual(load_provenance(run_directory)["state"], "CLOSED")

    def test_a_lookup_that_fails_keeps_the_state_it_had(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            run_directory = _filed_run(data_root, "20260907T173348Z-003915", 3884)
            before = load_provenance(run_directory)

            record, failure = refresh_state(run_directory, state_lookup=_unavailable)

            self.assertIn("pdm-project/pdm#3884", failure)
            self.assertIn("gh is not installed", failure)
            self.assertEqual(record["state"], "OPEN")
            self.assertEqual(load_provenance(run_directory), before)

    def test_refreshing_reports_every_run_it_could_not_read(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            _filed_run(data_root, "20260907T173348Z-003915", 3884)
            _filed_run(data_root, "20260907T223142Z-91e3e8", 3883)

            found, failures = refresh_contributions(
                data_root, state_lookup=_unavailable
            )

            self.assertEqual(len(found), 2)
            self.assertEqual(len(failures), 2)
            self.assertTrue(all(entry.state == "OPEN" for entry in found))

    def test_a_run_that_filed_nothing_is_left_alone(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            run_directory = data_root / "20260906T000000Z-cccccc"
            workspace, base = _repository(run_directory)
            record_provenance(
                run_id="20260906T000000Z-cccccc",
                run_directory=run_directory,
                repository="pdm-project/pdm",
                base_commit=base,
                workspace=workspace,
            )

            record, failure = refresh_state(run_directory, state_lookup=_unavailable)

            self.assertIsNone(failure)
            self.assertIsNone(record["state"])


class ReadingAgeTests(unittest.TestCase):
    def test_a_state_never_read_is_stale(self) -> None:
        self.assertTrue(state_is_stale(None))
        self.assertTrue(state_is_stale("not a timestamp"))

    def test_a_day_old_reading_is_stale_and_a_minute_old_one_is_not(self) -> None:
        now = datetime(2026, 9, 9, 8, 44, tzinfo=UTC)
        self.assertTrue(state_is_stale("2026-09-08T07:00:00+00:00", now=now))
        self.assertFalse(state_is_stale("2026-09-09T08:43:00+00:00", now=now))

    def test_the_listing_says_when_each_state_was_read(self) -> None:
        with TemporaryDirectory() as name:
            data_root = Path(name)
            _filed_run(data_root, "20260907T173348Z-003915", 3884)
            found = collect_contributions(data_root)

            fresh = render_contributions(
                found, now=datetime.now(UTC) + timedelta(minutes=1)
            )
            self.assertIn("state read", fresh)
            self.assertNotIn("stale", fresh)

            later = render_contributions(
                found, now=datetime.now(UTC) + timedelta(days=2)
            )
            self.assertIn("stale", later)
            self.assertIn("--refresh", later)

    def test_a_run_with_no_pull_request_gets_no_reading_line(self) -> None:
        rendered = render_contributions(
            [contribution_from_record({"run_id": "r", "repository": "pdm-project/pdm"})]
        )
        self.assertNotIn("state read", rendered)
        self.assertNotIn("stale", rendered)
