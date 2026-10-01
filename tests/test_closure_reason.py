"""Why a filed pull request closed without merging.

https://github.com/wolfgang-aura/Mailman/issues/79
"""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, ClassVar
from unittest.mock import patch

from mailman.cli import main
from mailman.provenance import (
    classify_closure,
    closure_counts,
    contribution_from_record,
    load_provenance,
    refresh_state,
    render_contributions,
)
from tests.test_provenance import _filed_run, _name_the_issue, _no_competitors, _open

# What `gh pr view` said of pdm-project/pdm#3884 once it closed, trimmed to the
# fields the classifier reads.
PDM_3884: dict[str, Any] = {
    "available": True,
    "state": "CLOSED",
    "created_at": "2026-09-08T20:43:00Z",
    "closed_at": "2026-09-09T07:06:29Z",
    "author": "wolfgang-aura",
}
SLUG = "pdm-project/pdm"
DC4E314 = "dc4e314" + "0" * 33
ISSUE = f"repos/{SLUG}/issues/3877/timeline"
PULL = f"repos/{SLUG}/issues/3884/timeline"
WINDOW = f"repos/{SLUG}/commits?since="


class _FakeGh:
    """Answers `gh api` list reads by path prefix; any other read fails the test.

    A value of None stands for a read that failed.
    """

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.paths: list[str] = []

    def __call__(self, path: str) -> tuple[Any, str | None]:
        self.paths.append(path)
        for prefix, value in self.answers.items():
            if path.startswith(prefix):
                if value is None:
                    return None, "HTTP 502"
                return value, None
        raise AssertionError(f"unexpected gh read {path}")


def _closed_by(sha: str) -> dict[str, Any]:
    return {
        "event": "closed",
        "commit_id": sha,
        "commit_url": f"https://api.github.com/repos/{SLUG}/commits/{sha}",
    }


def _issue_closed() -> dict[str, Any]:
    return {"event": "closed", "commit_id": None, "commit_url": None}


def _merged_reference(number: int) -> dict[str, Any]:
    return {
        "event": "cross-referenced",
        "source": {
            "issue": {
                "number": number,
                "state": "closed",
                "html_url": f"https://github.com/{SLUG}/pull/{number}",
                "repository_url": f"https://api.github.com/repos/{SLUG}",
                "created_at": "2026-09-09T00:00:00Z",
                "user": {"login": "frostming"},
                "pull_request": {"merged_at": "2026-09-09T06:00:00Z"},
            }
        },
    }


def _said(kind: str, login: str, association: str, **extra: Any) -> dict[str, Any]:
    return {
        "event": kind,
        "user": {"login": login, "type": "User"},
        "author_association": association,
        "html_url": f"https://github.com/{SLUG}/pull/3884#{kind}",
        **extra,
    }


def _classify(answers: dict[str, Any], **overrides: Any) -> tuple[dict[str, Any], _FakeGh]:
    gh = _FakeGh(answers)
    arguments: dict[str, Any] = {"issue_number": 3877, "pull": PDM_3884, "api": gh}
    arguments.update(overrides)
    return classify_closure(SLUG, 3884, **arguments), gh


class ClosureReasonTests(unittest.TestCase):
    def test_a_merged_pull_request_on_the_same_issue_supersedes_ours(self) -> None:
        closure, gh = _classify({ISSUE: [_merged_reference(3890), _issue_closed()]})

        self.assertEqual(closure["reason"], "superseded-by-pr")
        self.assertEqual(closure["pull_request"], 3890)
        self.assertEqual(gh.paths, [ISSUE])

    def test_a_merged_pull_request_that_left_the_issue_open_does_not_supersede_ours(
        self,
    ) -> None:
        # Mailman #363: a refactor the issue was split off from merged and left it open.
        closure, _ = _classify({ISSUE: [_merged_reference(3890)], PULL: [], WINDOW: []})

        self.assertEqual(closure["reason"], "closed-silently")

    def test_an_issue_closed_by_a_commit_no_pull_request_carried_is_direct(self) -> None:
        """pdm-project/pdm#3884: the maintainer committed dc4e314 on main."""
        closure, _ = _classify(
            {
                ISSUE: [_closed_by(DC4E314)],
                PULL: [],
                f"repos/{SLUG}/commits/{DC4E314}/pulls": [],
            }
        )

        self.assertEqual(closure["reason"], "maintainer-direct-commit")
        self.assertEqual(closure["commit"], DC4E314)
        self.assertIn("dc4e31400000", closure["detail"])

    def test_a_default_branch_commit_naming_the_issue_after_filing_is_direct(
        self,
    ) -> None:
        commit = {"sha": DC4E314, "commit": {"message": "docs: fix pep621 (#3877)"}}
        unrelated = {"sha": "e" * 40, "commit": {"message": "chore: bump #38770"}}
        closure, gh = _classify(
            {
                ISSUE: [],
                PULL: [],
                WINDOW: [unrelated, commit],
                f"repos/{SLUG}/commits/{DC4E314}/pulls": [],
            }
        )

        self.assertEqual(closure["reason"], "maintainer-direct-commit")
        window = next(path for path in gh.paths if path.startswith(WINDOW))
        self.assertIn("since=2026-09-08T20:43:00Z", window)
        # A day past the close, for a fix pushed just after ours was closed.
        self.assertIn("until=2026-09-10T07:06:29Z", window)
        self.assertNotIn(f"repos/{SLUG}/commits/{'e' * 40}/pulls", gh.paths)

    def test_a_closing_commit_a_merged_pull_request_carried_is_a_supersede(
        self,
    ) -> None:
        closure, _ = _classify(
            {
                ISSUE: [_closed_by(DC4E314)],
                PULL: [],
                f"repos/{SLUG}/commits/{DC4E314}/pulls": [
                    {
                        "number": 3890,
                        "merged_at": "2026-09-09T06:00:00Z",
                        "html_url": f"https://github.com/{SLUG}/pull/3890",
                    }
                ],
            }
        )

        self.assertEqual(closure["reason"], "superseded-by-pr")
        self.assertEqual(closure["pull_request"], 3890)

    def test_a_rejecting_review_outranks_a_direct_commit(self) -> None:
        closure, gh = _classify(
            {
                ISSUE: [_closed_by(DC4E314)],
                PULL: [_said("reviewed", "frostming", "OWNER", state="CHANGES_REQUESTED")],
            }
        )

        self.assertEqual(closure["reason"], "closed-with-review")
        self.assertIn("frostming reviewed (changes_requested)", closure["detail"])
        self.assertNotIn(f"repos/{SLUG}/commits/{DC4E314}/pulls", gh.paths)

    def test_a_maintainer_comment_counts_and_ours_bots_and_strangers_do_not(
        self,
    ) -> None:
        ours = _said("commented", "wolfgang-aura", "CONTRIBUTOR")
        bot = _said("commented", "codecov[bot]", "NONE")
        stranger = _said("commented", "someone", "NONE")
        answers = {ISSUE: [], WINDOW: []}

        closure, _ = _classify({**answers, PULL: [ours, bot, stranger]})
        self.assertEqual(closure["reason"], "closed-silently")

        maintainer = _said("commented", "frostming", "MEMBER")
        closure, _ = _classify({**answers, PULL: [ours, maintainer]})
        self.assertEqual(closure["reason"], "closed-with-comment")
        self.assertIn("frostming", closure["detail"])

    def test_every_failed_read_is_unknown_never_silent(self) -> None:
        cases = {
            "issue timeline": {ISSUE: None},
            "pull request timeline": {ISSUE: [], PULL: None},
            "closing commit": {
                ISSUE: [_closed_by(DC4E314)],
                PULL: [],
                f"repos/{SLUG}/commits/{DC4E314}/pulls": None,
            },
            "commit window": {ISSUE: [], PULL: [], WINDOW: None},
        }
        for name, answers in cases.items():
            with self.subTest(name):
                closure, _ = _classify(answers)
                self.assertEqual(closure["reason"], "unknown")
                self.assertIn("HTTP 502", closure["detail"])

    def test_missing_inputs_are_unknown_without_a_read(self) -> None:
        closure, gh = _classify({}, issue_number=None)
        self.assertEqual(closure["reason"], "unknown")
        self.assertIn("names no issue", closure["detail"])
        self.assertEqual(gh.paths, [])

        closure, gh = _classify({}, pull={"state": "CLOSED"})
        self.assertEqual(closure["reason"], "unknown")
        self.assertEqual(gh.paths, [])


class _Lookup:
    """A stand-in classifier that answers one reason and counts its calls."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.calls: list[int | None] = []

    def __call__(
        self, repository: str, number: int, *, issue_number: int | None, pull: dict
    ) -> dict[str, Any]:
        self.calls.append(issue_number)
        return {"reason": self.reason, "detail": "read"}


def _pdm_closed(repository: str, number: int) -> dict[str, Any]:
    return {**PDM_3884, "url": f"https://github.com/{repository}/pull/{number}"}


def _closed_entry(run_id: str, number: int, reason: str | None) -> Any:
    record: dict[str, Any] = {
        "run_id": run_id,
        "repository": SLUG,
        "pull_request": number,
        "state": "CLOSED",
        "checked_at": "2026-09-16T00:00:00+00:00",
    }
    if reason:
        record["closure"] = {"reason": reason, "detail": "why"}
    return contribution_from_record(record)


class ClosureRefreshTests(unittest.TestCase):
    def test_a_refresh_stores_the_reason_and_keeps_a_settled_one(self) -> None:
        with TemporaryDirectory() as name:
            run_directory = _filed_run(Path(name), "20260907T173348Z-003915", 3884)
            _name_the_issue(run_directory, f"https://github.com/{SLUG}/issues/3877")
            first = _Lookup("maintainer-direct-commit")

            _, failure = refresh_state(
                run_directory, state_lookup=_pdm_closed, closure_lookup=first
            )

            self.assertIsNone(failure)
            self.assertEqual(first.calls, [3877])
            stored = load_provenance(run_directory)["closure"]
            self.assertEqual(stored["reason"], "maintainer-direct-commit")
            self.assertIn("checked_at", stored)

            again = _Lookup("closed-silently")
            refresh_state(run_directory, state_lookup=_pdm_closed, closure_lookup=again)
            self.assertEqual(again.calls, [])
            self.assertEqual(
                load_provenance(run_directory)["closure"]["reason"],
                "maintainer-direct-commit",
            )

    def test_an_unknown_reason_is_a_failure_and_is_read_again(self) -> None:
        with TemporaryDirectory() as name:
            run_directory = _filed_run(Path(name), "20260907T173348Z-003915", 3884)

            _, failure = refresh_state(
                run_directory, state_lookup=_pdm_closed, closure_lookup=_Lookup("unknown")
            )

            self.assertIn("closure reason unknown", failure)
            later = _Lookup("closed-silently")
            refresh_state(run_directory, state_lookup=_pdm_closed, closure_lookup=later)
            self.assertEqual(len(later.calls), 1)
            self.assertEqual(
                load_provenance(run_directory)["closure"]["reason"], "closed-silently"
            )

    def test_an_open_pull_request_has_no_closure(self) -> None:
        with TemporaryDirectory() as name:
            run_directory = _filed_run(Path(name), "20260907T173348Z-003915", 3884)
            never = _Lookup("closed-silently")

            record, _ = refresh_state(
                run_directory,
                state_lookup=_open,
                competitor_lookup=_no_competitors,
                closure_lookup=never,
            )

            self.assertIsNone(record["closure"])
            self.assertEqual(never.calls, [])


class ClosureListingTests(unittest.TestCase):
    FOUND: ClassVar[list[Any]] = [
        _closed_entry("a", 1, "maintainer-direct-commit"),
        _closed_entry("b", 2, "superseded-by-pr"),
        _closed_entry("c", 3, "superseded-by-pr"),
        _closed_entry("d", 4, None),
        contribution_from_record(
            {"run_id": "e", "repository": SLUG, "pull_request": 5, "state": "MERGED"}
        ),
    ]

    def test_the_listing_shows_each_reason_and_a_count_per_reason(self) -> None:
        rendered = render_contributions(self.FOUND)

        self.assertIn("closed: maintainer-direct-commit -- why", rendered)
        self.assertIn("closure reason never read", rendered)
        self.assertIn(
            "closed unmerged, by reason: superseded-by-pr 2, "
            "maintainer-direct-commit 1, unread 1",
            rendered,
        )
        self.assertEqual(
            closure_counts(self.FOUND),
            {"superseded-by-pr": 2, "maintainer-direct-commit": 1, "unread": 1},
        )

    def test_the_json_listing_carries_the_reason_and_the_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            data_root.mkdir()
            with patch(
                "mailman.cli.refresh_contributions", return_value=(self.FOUND, [])
            ):
                out = StringIO()
                with redirect_stdout(out), redirect_stderr(StringIO()):
                    main(
                        [
                            "contributions",
                            "--refresh",
                            "--json",
                            "--data-root",
                            str(data_root),
                        ]
                    )
            payload = json.loads(out.getvalue().split("\n\n", 1)[0])
            self.assertEqual(payload["closure_counts"]["superseded-by-pr"], 2)
            self.assertEqual(
                payload["contributions"][0]["closure"]["reason"],
                "maintainer-direct-commit",
            )


if __name__ == "__main__":
    unittest.main()
