from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mailman.targeting import (
    ALREADY_FIXED_UPSTREAM,
    BUG_NOT_REPRODUCED,
    MERGED_FIX_ALREADY_IN_BASE,
    ISSUE_ASSIGNED,
    NO_CLAIM_CHECK,
    NO_DUPLICATE_SEARCH,
    OPEN_PULL_REQUEST,
    UNACKNOWLEDGED_ATTEMPTS,
    UNACKNOWLEDGED_CLAIM,
    WORK_HANDED_OVER,
    assess_target,
)


def _record(
    root: Path,
    *,
    searched: bool = True,
    attempts: list | None = None,
    intel: bool = True,
    fresh: bool = True,
    reproduced: bool | None = True,
    claims: list | None = None,
    assignments: list | None = None,
    assignees: list | None = None,
    claims_read: bool = True,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    if claims_read:
        (root / "claims.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "repository": "example/project",
                    "issue_number": 4775,
                    "comments_read": 2,
                    "claims": claims or [],
                    "assignments": assignments or [],
                    "assignees": assignees or [],
                }
            ),
            encoding="utf-8",
        )
    if reproduced is not None:
        (root / "reproduction.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "machine_checked": True,
                    "reproduced": reproduced,
                    "checks": [],
                }
            ),
            encoding="utf-8",
        )
    (root / "duplicate-search.json").write_text(
        json.dumps({"success": searched, "matches": []}), encoding="utf-8"
    )
    if intel:
        (root / "target-intel.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "repository": "example/project",
                    "window_days": 14,
                    "freshness": {
                        "human_outside_merges": 4 if fresh else 0,
                        "outside_pull_requests_closed_unmerged": 2,
                    },
                    "assessment": {
                        "passes_freshness_bar": fresh,
                        "assignment_looks_required": False,
                        "automated_enforcement": [],
                    },
                }
            ),
            encoding="utf-8",
        )
    if attempts is not None:
        (root / "prior-art.json").write_text(
            json.dumps({"success": True, "attempts": attempts}), encoding="utf-8"
        )
    return root


_OPEN = {
    "number": 14668,
    "title": "Handle RaisesGroup check errors during suggestions",
    "outcome": "open",
    "url": "https://github.com/pytest-dev/pytest/pull/14668",
}
_CLOSED = {
    "number": 14502,
    "title": "Handle group-only RaisesGroup checks safely",
    "outcome": "closed unmerged",
    "url": "https://github.com/pytest-dev/pytest/pull/14502",
}
_MERGED = {
    "number": 14098,
    "title": "fix #14004 - connect conftests to nodeids/nodes",
    "outcome": "merged",
    "url": "https://github.com/pytest-dev/pytest/pull/14098",
}


_CLAIM = {
    "author": "someone",
    "association": "NONE",
    "created_at": "2026-09-01T00:00:00Z",
    "quote": "I'd like to work on this issue.",
}
_ASSIGNMENT = {
    "author": "maintainer",
    "association": "MEMBER",
    "created_at": "2026-09-01T01:00:00Z",
    "quote": "Go ahead, all yours",
}


class AssessTargetTests(unittest.TestCase):
    def test_an_unsearched_target_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(Path(temporary))

        self.assertFalse(assessment.may_start)
        self.assertIn(NO_DUPLICATE_SEARCH, assessment.blocking)
        self.assertIn("duplicate-search", assessment.summary())

    def test_a_failed_search_does_not_count_as_a_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(_record(Path(temporary), searched=False))

        self.assertIn(NO_DUPLICATE_SEARCH, assessment.blocking)

    def test_an_unclaimed_issue_may_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(_record(Path(temporary), attempts=[]))

        self.assertTrue(assessment.may_start)
        self.assertEqual(assessment.blocking, [])
        self.assertIn("unclaimed", assessment.summary())

    def test_a_searched_issue_with_no_prior_art_record_may_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(_record(Path(temporary)))

        self.assertTrue(assessment.may_start)

    def test_an_open_pull_request_refuses_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(_record(Path(temporary), attempts=[_OPEN]))

        self.assertFalse(assessment.may_start)
        self.assertIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertIn("14668", assessment.summary())

    def test_an_open_pull_request_refuses_even_when_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[_OPEN]), acknowledged=True
            )

        self.assertFalse(assessment.may_start)
        self.assertIn(OPEN_PULL_REQUEST, assessment.blocking)

    def test_closed_attempts_stop_a_run_until_someone_has_read_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(_record(Path(temporary), attempts=[_CLOSED]))

        self.assertFalse(assessment.may_start)
        self.assertIn(UNACKNOWLEDGED_ATTEMPTS, assessment.blocking)
        self.assertIn("rejected the approach", assessment.summary())

    def test_acknowledged_closed_attempts_may_start_and_stay_on_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[_CLOSED]), acknowledged=True
            )

        self.assertTrue(assessment.may_start)
        self.assertIn(UNACKNOWLEDGED_ATTEMPTS, assessment.warnings)
        self.assertEqual(len(assessment.closed_attempts), 1)

    def test_the_pytest_case_that_cost_three_runs_is_refused(self) -> None:
        # pytest-dev/pytest #14324: three closed attempts and one open since
        # July. Every one of the three runs on 2026-09-02 should have been
        # refused before a workspace was ever cloned.
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[_CLOSED, _OPEN]),
                acknowledged=True,
            )

        self.assertFalse(assessment.may_start)
        self.assertIn(OPEN_PULL_REQUEST, assessment.blocking)

    def test_a_merged_attempt_refuses_the_run_as_already_fixed_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(_record(Path(temporary), attempts=[_MERGED]))

        self.assertFalse(assessment.may_start)
        self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)
        self.assertEqual(assessment.merged_attempts, [_MERGED])
        self.assertEqual(assessment.closed_attempts, [])

    def test_a_merged_attempt_refuses_even_when_acknowledged(self) -> None:
        # `--acknowledge-prior-attempts` answers "someone tried and the
        # maintainers said no". It has no answer for "upstream already ships
        # this", so it must not clear one.
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[_MERGED]), acknowledged=True
            )

        self.assertFalse(assessment.may_start)
        self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)
        self.assertNotIn(UNACKNOWLEDGED_ATTEMPTS, assessment.blocking)
        self.assertNotIn(UNACKNOWLEDGED_ATTEMPTS, assessment.warnings)

    def test_a_merged_attempt_is_not_reported_as_a_rejection(self) -> None:
        # Run 20260903T045152Z-369e77 printed "closed #14098" and told the
        # operator the maintainers had rejected it. Both pull requests were
        # merged. See https://github.com/wolfgang-aura/Mailman/issues/38.
        with tempfile.TemporaryDirectory() as temporary:
            summary = assess_target(
                _record(Path(temporary), attempts=[_MERGED])
            ).summary()

        self.assertIn("merged    #14098", summary)
        self.assertNotIn("closed    #14098", summary)
        self.assertNotIn("rejected the approach", summary)
        self.assertIn("already upstream", summary)

    def test_a_merged_attempt_alongside_a_closed_one_reports_both_truthfully(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[_MERGED, _CLOSED]),
                acknowledged=True,
            )
        summary = assessment.summary()

        self.assertFalse(assessment.may_start)
        self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)
        self.assertEqual(assessment.merged_attempts, [_MERGED])
        self.assertEqual(assessment.closed_attempts, [_CLOSED])
        self.assertIn("merged    #14098", summary)
        self.assertIn("closed    #14502", summary)
        self.assertIn("rejected the approach", summary)

    def test_a_merged_attempt_is_not_an_unclaimed_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            summary = assess_target(
                _record(Path(temporary), attempts=[_MERGED])
            ).summary()

        self.assertNotIn("unclaimed", summary)

    def test_the_assessment_record_carries_the_merged_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recorded = assess_target(
                _record(Path(temporary), attempts=[_MERGED])
            ).to_dict()

        self.assertEqual(recorded["merged_attempts"], [_MERGED])
        self.assertEqual(recorded["closed_attempts"], [])
        self.assertFalse(recorded["may_start"])

    def test_an_unread_comment_thread_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[], claims_read=False)
            )

        self.assertFalse(assessment.may_start)
        self.assertIn(NO_CLAIM_CHECK, assessment.blocking)
        self.assertIn("mailman claims", assessment.summary())

    def test_a_claim_in_the_comments_refuses_an_otherwise_clean_target(self) -> None:
        # openai/openai-agents-python #4775: no pull request against it, so the
        # duplicate search was empty and the target read as unclaimed. See
        # https://github.com/wolfgang-aura/Mailman/issues/36.
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[], claims=[_CLAIM])
            )
        summary = assessment.summary()

        self.assertFalse(assessment.may_start)
        self.assertIn(UNACKNOWLEDGED_CLAIM, assessment.blocking)
        self.assertNotIn("unclaimed", summary)
        self.assertIn("someone", summary)
        self.assertIn("work on this issue", summary)

    def test_an_unanswered_claim_may_be_acknowledged(self) -> None:
        # An offer nobody answered is worth a human reading, not a hard stop.
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[], claims=[_CLAIM]),
                acknowledged_claims=True,
            )

        self.assertTrue(assessment.may_start)
        self.assertIn(UNACKNOWLEDGED_CLAIM, assessment.warnings)

    def test_acknowledging_prior_attempts_does_not_acknowledge_a_claim(self) -> None:
        # Two different questions, so two different flags.
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[], claims=[_CLAIM]),
                acknowledged=True,
            )

        self.assertFalse(assessment.may_start)
        self.assertIn(UNACKNOWLEDGED_CLAIM, assessment.blocking)

    def test_a_maintainer_handing_the_work_over_refuses_outright(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(
                    Path(temporary),
                    attempts=[],
                    claims=[_CLAIM],
                    assignments=[_ASSIGNMENT],
                ),
                acknowledged_claims=True,
            )

        self.assertFalse(assessment.may_start)
        self.assertIn(WORK_HANDED_OVER, assessment.blocking)
        self.assertIn("maintainer", assessment.summary())

    def test_an_assigned_issue_refuses_outright(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[], assignees=["maintainer"]),
                acknowledged_claims=True,
            )

        self.assertFalse(assessment.may_start)
        self.assertIn(ISSUE_ASSIGNED, assessment.blocking)

    def test_a_read_thread_with_no_claim_still_looks_unclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(_record(Path(temporary), attempts=[]))
        summary = assessment.summary()

        self.assertTrue(assessment.may_start)
        self.assertIn("unclaimed", summary)
        self.assertIn("2 comment(s)", summary)

    def test_the_assessment_record_carries_the_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            recorded = assess_target(
                _record(Path(temporary), attempts=[], claims=[_CLAIM])
            ).to_dict()

        self.assertEqual(recorded["claims"]["claims"], [_CLAIM])

    def test_a_malformed_record_is_treated_as_no_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "duplicate-search.json").write_text("{not json", encoding="utf-8")

            assessment = assess_target(root)

        self.assertIn(NO_DUPLICATE_SEARCH, assessment.blocking)


if __name__ == "__main__":
    unittest.main()


def _git(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


class MergedFixAlreadyInBaseTests(unittest.TestCase):
    """A merged match that is already in the base commit is not this change.

    See https://github.com/wolfgang-aura/Mailman/issues/46. A defect report
    names the function it lives in, so any past fix to that function is a
    whole-query match, and `already-fixed-upstream` had no way to say that the
    accepted fix is already in the tree the reproduction failed against.
    """

    def _workspace(self, root: Path) -> tuple[str, str]:
        """Build a two commit clone and return (first commit, base commit)."""
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        _git(workspace, "init", "--quiet")
        _git(workspace, "config", "user.email", "test@example.com")
        _git(workspace, "config", "user.name", "Test")
        (workspace / "core.py").write_text("first\n", encoding="utf-8")
        _git(workspace, "add", "core.py")
        _git(workspace, "commit", "--quiet", "-m", "the merged fix")
        merged = _git(workspace, "rev-parse", "HEAD")
        (workspace / "core.py").write_text("second\n", encoding="utf-8")
        _git(workspace, "add", "core.py")
        _git(workspace, "commit", "--quiet", "-m", "later work")
        base = _git(workspace, "rev-parse", "HEAD")
        return merged, base

    def _prepare(
        self,
        root: Path,
        *,
        merge_commit: str | None,
        base_commit: str,
        recorded_head: str | None,
        reproduced: bool = True,
    ) -> Path:
        attempt = dict(_MERGED)
        attempt["merge_commit"] = merge_commit
        _record(root, attempts=[attempt])
        (root / "reproduction.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "machine_checked": True,
                    "reproduced": reproduced,
                    "base_commit": base_commit,
                    "checks": [],
                }
            ),
            encoding="utf-8",
        )
        if recorded_head is not None:
            (root / "workspace.json").write_text(
                json.dumps({"head": recorded_head, "clean": True}), encoding="utf-8"
            )
        return root

    def test_a_merge_already_in_the_base_commit_does_not_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            merged, base = self._workspace(root)
            assessment = assess_target(
                self._prepare(
                    root, merge_commit=merged, base_commit=base, recorded_head=base
                )
            )
            self.assertNotIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)
            self.assertIn(MERGED_FIX_ALREADY_IN_BASE, assessment.warnings)
            self.assertEqual([], assessment.merged_attempts)
            self.assertEqual(
                [14098], [row["number"] for row in assessment.superseded_attempts]
            )
            self.assertTrue(assessment.may_start)

    def test_the_summary_names_the_merge_it_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            merged, base = self._workspace(root)
            summary = assess_target(
                self._prepare(
                    root, merge_commit=merged, base_commit=base, recorded_head=base
                )
            ).summary()
            self.assertIn("in base   #14098", summary)
            self.assertIn("already an ancestor of the base commit", summary)
            self.assertNotIn("This target looks unclaimed", summary)

    def test_a_merge_that_is_not_an_ancestor_still_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, base = self._workspace(root)
            unrelated = "0" * 40
            assessment = assess_target(
                self._prepare(
                    root, merge_commit=unrelated, base_commit=base, recorded_head=base
                )
            )
            self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)
            self.assertEqual([], assessment.superseded_attempts)

    def test_a_merge_with_no_recorded_commit_still_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, base = self._workspace(root)
            assessment = assess_target(
                self._prepare(
                    root, merge_commit=None, base_commit=base, recorded_head=base
                )
            )
            self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)

    def test_a_reproduction_from_another_commit_does_not_clear_it(self) -> None:
        """The clone must be standing at the commit the reproduction ran at."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            merged, base = self._workspace(root)
            assessment = assess_target(
                self._prepare(
                    root,
                    merge_commit=merged,
                    base_commit=merged,
                    recorded_head=base,
                )
            )
            self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)

    def test_no_workspace_record_does_not_clear_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            merged, base = self._workspace(root)
            assessment = assess_target(
                self._prepare(
                    root, merge_commit=merged, base_commit=base, recorded_head=None
                )
            )
            self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)

    def test_a_bug_that_did_not_reproduce_does_not_clear_it(self) -> None:
        """Nothing clears a merged match when the defect no longer happens."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            merged, base = self._workspace(root)
            assessment = assess_target(
                self._prepare(
                    root,
                    merge_commit=merged,
                    base_commit=base,
                    recorded_head=base,
                    reproduced=False,
                )
            )
            self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)
            self.assertIn(BUG_NOT_REPRODUCED, assessment.blocking)
