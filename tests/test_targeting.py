from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailman.targeting import (
    ALREADY_FIXED_UPSTREAM,
    STALE_PRIOR_ATTEMPT,
    BUG_NOT_REPRODUCED,
    DUPLICATE_FORBIDDEN_OPEN_ATTEMPT,
    MAINTAINER_CLOSED_ATTEMPT,
    ISSUE_ASSIGNED,
    MERGED_FIX_ALREADY_IN_BASE,
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
#: The same attempt, written by somebody who speaks for the project. A
#: maintainer's own closed branch still has to be read and acknowledged.
_CLOSED_BY_MAINTAINER = {**_CLOSED, "author_association": "MEMBER"}
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

    def test_a_strong_open_search_match_refuses_without_prior_art(self) -> None:
        # The duplicate search can already prove that a live rival exists. A
        # missing `prior-art.json` must not turn that proof into permission to
        # start two agents and an environment build.
        with tempfile.TemporaryDirectory() as temporary:
            root = _record(Path(temporary), attempts=None)
            (root / "duplicate-search.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "complete": True,
                        "matches": [
                            {
                                "number": 2330,
                                "title": "fix(backtest): handle IndexError at right calendar boundary",
                                "state": "OPEN",
                                "url": "https://github.com/microsoft/qlib/pull/2330",
                                "pull_request": True,
                                "matched_by": ["search", "#2278"],
                                "methods": ["search"],
                                "references_issue": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            assessment = assess_target(root)

        self.assertFalse(assessment.may_start)
        self.assertIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertIn("2330", assessment.summary())

    def test_a_strong_open_issue_match_is_not_an_attempt(self) -> None:
        # skfolio#312 is a tracking issue that cross-references #307. It matched
        # strongly and was reported as an open pull request, refusing a target
        # with no attempt on it. Only a pull request is an attempt.
        with tempfile.TemporaryDirectory() as temporary:
            root = _record(Path(temporary), attempts=None)
            (root / "duplicate-search.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "complete": True,
                        "matches": [
                            {
                                "number": 312,
                                "title": "Code quality assessment at v1.0.6",
                                "state": "OPEN",
                                "url": "https://github.com/skfolio/skfolio/issues/312",
                                "pull_request": False,
                                "matched_by": ["search", "#307"],
                                "methods": ["search"],
                                "references_issue": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            assessment = assess_target(root)

        self.assertNotIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertEqual(assessment.open_attempts, [])

    def test_a_maintainers_closed_search_match_needs_prior_art_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = _record(Path(temporary), attempts=None)
            (root / "duplicate-search.json").write_text(
                json.dumps(
                    {
                        "success": True,
                        "complete": True,
                        "matches": [
                            {
                                "number": 13534,
                                "title": "fix: isolate scheduled maintenance jobs",
                                "state": "CLOSED",
                                "url": "https://github.com/example/project/pull/13534",
                                "pull_request": True,
                                "author_association": "OWNER",
                                "matched_by": ["search", "#4775"],
                                "methods": ["search"],
                                "references_issue": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            assessment = assess_target(root)

        self.assertFalse(assessment.may_start)
        self.assertIn(UNACKNOWLEDGED_ATTEMPTS, assessment.blocking)
        self.assertIn("13534", assessment.summary())

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

    def test_a_maintainers_closed_attempt_stops_a_run_until_it_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[_CLOSED_BY_MAINTAINER])
            )

        self.assertFalse(assessment.may_start)
        self.assertIn(UNACKNOWLEDGED_ATTEMPTS, assessment.blocking)
        self.assertIn("rejected the approach", assessment.summary())

    def test_acknowledged_closed_attempts_may_start_and_stay_on_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            assessment = assess_target(
                _record(Path(temporary), attempts=[_CLOSED_BY_MAINTAINER]),
                acknowledged=True,
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
                _record(Path(temporary), attempts=[_CLOSED_BY_MAINTAINER, _OPEN]),
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
                _record(
                    Path(temporary), attempts=[_MERGED, _CLOSED_BY_MAINTAINER]
                ),
                acknowledged=True,
            )
        summary = assessment.summary()

        self.assertFalse(assessment.may_start)
        self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)
        self.assertEqual(assessment.merged_attempts, [_MERGED])
        self.assertEqual(assessment.closed_attempts, [_CLOSED_BY_MAINTAINER])
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


#: Every staleness test measures against this instant, so the verdicts do not
#: change with the calendar.
_NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def _open_attempt(*, days_since_activity: float, **extra: object) -> dict:
    """An open pull request last touched this many days before `_NOW`."""
    last = _NOW - timedelta(days=days_since_activity)
    return {
        "number": 14668,
        "title": "Handle RaisesGroup check errors during suggestions",
        "outcome": "open",
        "url": "https://github.com/pytest-dev/pytest/pull/14668",
        "created_at": (last - timedelta(days=30)).isoformat(),
        "updated_at": last.isoformat(),
        **extra,
    }


class StaleAttemptTests(unittest.TestCase):
    """An attempt that stopped moving is prior art, not a claim.

    Decided by the operator on 2026-09-17, after a hunt lost 40 of 66
    pre-screens to `open-pull-request`.
    """

    def _assess(self, attempts: list[dict]):
        with tempfile.TemporaryDirectory() as temporary:
            return assess_target(
                _record(Path(temporary), attempts=attempts), now=_NOW
            )

    def test_an_open_attempt_at_sixty_one_days_is_stale(self) -> None:
        assessment = self._assess([_open_attempt(days_since_activity=61)])

        self.assertTrue(assessment.may_start)
        self.assertNotIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertIn(STALE_PRIOR_ATTEMPT, assessment.warnings)
        self.assertEqual(assessment.open_attempts, [])
        self.assertEqual(len(assessment.stale_attempts), 1)
        row = assessment.stale_attempts[0]
        self.assertEqual(row["number"], 14668)
        self.assertEqual(row["state"], "open")
        self.assertEqual(row["days_stale"], 61.0)
        self.assertEqual(
            row["url"], "https://github.com/pytest-dev/pytest/pull/14668"
        )
        self.assertIn("supersedes it", assessment.summary())

    def test_an_open_attempt_at_fifty_nine_days_still_claims_the_issue(self) -> None:
        assessment = self._assess([_open_attempt(days_since_activity=59)])

        self.assertFalse(assessment.may_start)
        self.assertIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertEqual(assessment.stale_attempts, [])

    def test_exactly_sixty_days_is_already_stale(self) -> None:
        assessment = self._assess([_open_attempt(days_since_activity=60)])

        self.assertTrue(assessment.may_start)
        self.assertIn(STALE_PRIOR_ATTEMPT, assessment.warnings)

    def test_an_open_attempt_with_no_timestamp_still_claims_the_issue(self) -> None:
        # Dormancy that cannot be proved is not dormancy. Silence in the
        # record must read as a live claim.
        assessment = self._assess([_OPEN])

        self.assertFalse(assessment.may_start)
        self.assertIn(OPEN_PULL_REQUEST, assessment.blocking)

    def test_a_pull_request_closed_without_merging_is_stale(self) -> None:
        assessment = self._assess([_CLOSED])

        self.assertTrue(assessment.may_start)
        self.assertNotIn(UNACKNOWLEDGED_ATTEMPTS, assessment.blocking)
        self.assertIn(STALE_PRIOR_ATTEMPT, assessment.warnings)
        self.assertEqual(assessment.closed_attempts, [])
        self.assertEqual(assessment.stale_attempts[0]["state"], "closed unmerged")

    def test_a_merged_attempt_is_never_stale(self) -> None:
        assessment = self._assess([_MERGED])

        self.assertFalse(assessment.may_start)
        self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)
        self.assertEqual(assessment.stale_attempts, [])

    def test_a_maintainers_dormant_attempt_still_blocks(self) -> None:
        # An OWNER or MEMBER branch is the project's own work in progress,
        # however long it has sat there.
        assessment = self._assess(
            [_open_attempt(days_since_activity=400, author_association="MEMBER")]
        )

        self.assertFalse(assessment.may_start)
        self.assertIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertEqual(assessment.stale_attempts, [])

    def test_an_outside_contributors_dormant_attempt_does_not_block(self) -> None:
        assessment = self._assess(
            [_open_attempt(days_since_activity=400, author_association="CONTRIBUTOR")]
        )

        self.assertTrue(assessment.may_start)
        self.assertIn(STALE_PRIOR_ATTEMPT, assessment.warnings)

    def test_created_at_stands_in_when_there_is_no_update(self) -> None:
        assessment = self._assess(
            [
                {
                    "number": 14668,
                    "outcome": "open",
                    "title": "Handle RaisesGroup check errors",
                    "url": "https://github.com/pytest-dev/pytest/pull/14668",
                    "created_at": (_NOW - timedelta(days=200)).isoformat(),
                }
            ]
        )

        self.assertTrue(assessment.may_start)
        self.assertEqual(assessment.stale_attempts[0]["days_stale"], 200.0)


class MaintainerClosedAttemptTests(unittest.TestCase):
    """A rejection is not a dormant branch, whatever its age."""

    def _assess(self, attempts: list[dict]):
        with tempfile.TemporaryDirectory() as temporary:
            return assess_target(
                _record(Path(temporary), attempts=attempts), now=_NOW
            )

    def _closed(self, **closure: object) -> dict:
        return {
            "number": 307,
            "title": "Handle the empty portfolio",
            "outcome": "closed unmerged",
            "url": "https://github.com/skfolio/skfolio/pull/307",
            "author": "outsider",
            "updated_at": (_NOW - timedelta(days=120)).isoformat(),
            **closure,
        }

    def test_an_attempt_a_maintainer_closed_blocks(self) -> None:
        assessment = self._assess(
            [
                self._closed(
                    closed_by={
                        "login": "maintainer",
                        "association": "OWNER",
                        "maintainer": True,
                        "detail": "maintainer (owner) closed it, and did not write it",
                    }
                )
            ]
        )

        self.assertFalse(assessment.may_start)
        self.assertIn(MAINTAINER_CLOSED_ATTEMPT, assessment.blocking)
        self.assertNotIn(STALE_PRIOR_ATTEMPT, assessment.warnings)
        self.assertNotIn(UNACKNOWLEDGED_ATTEMPTS, assessment.blocking)
        self.assertEqual(assessment.stale_attempts, [])
        self.assertEqual(assessment.closed_attempts, [])
        self.assertEqual(assessment.maintainer_closed_attempts[0]["number"], 307)
        self.assertIn("said no", assessment.summary())

    def test_an_author_closing_their_own_attempt_is_stale(self) -> None:
        assessment = self._assess(
            [
                self._closed(
                    closed_by={
                        "login": "outsider",
                        "association": "CONTRIBUTOR",
                        "maintainer": False,
                        "detail": "outsider closed their own pull request",
                    }
                )
            ]
        )

        self.assertTrue(assessment.may_start)
        self.assertIn(STALE_PRIOR_ATTEMPT, assessment.warnings)
        self.assertEqual(assessment.maintainer_closed_attempts, [])

    def test_an_unknown_closer_keeps_the_old_behaviour(self) -> None:
        assessment = self._assess([self._closed()])

        self.assertTrue(assessment.may_start)
        self.assertIn(STALE_PRIOR_ATTEMPT, assessment.warnings)
        self.assertIsNone(assessment.stale_attempts[0]["closed_by"])


class DuplicateForbiddenTests(unittest.TestCase):
    """Where a second pull request is refused unread, dormancy clears nothing.

    urllib3: "Duplicate pull requests for the same issue, including
    alternative solutions, will be rejected without review unless a maintainer
    has approved opening an alternative pull request in advance."
    """

    def _assess(self, attempts: list[dict], *, forbids: bool = True):
        with tempfile.TemporaryDirectory() as temporary:
            return assess_target(
                _record(Path(temporary), attempts=attempts),
                now=_NOW,
                forbids_duplicates=forbids,
            )

    def test_a_dormant_open_attempt_blocks_under_its_own_code(self) -> None:
        assessment = self._assess([_open_attempt(days_since_activity=400)])

        self.assertFalse(assessment.may_start)
        self.assertIn(DUPLICATE_FORBIDDEN_OPEN_ATTEMPT, assessment.blocking)
        # Its own code, not the generic one: the reason is the repository's
        # rule, not a rival who is still working.
        self.assertNotIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertNotIn(STALE_PRIOR_ATTEMPT, assessment.warnings)
        self.assertEqual(assessment.stale_attempts, [])
        self.assertEqual(assessment.duplicate_blocked_attempts[0]["number"], 14668)
        self.assertIn("nothing to supersede here", assessment.summary())

    def test_a_closed_unmerged_attempt_is_unaffected(self) -> None:
        assessment = self._assess([_CLOSED])

        self.assertTrue(assessment.may_start)
        self.assertIn(STALE_PRIOR_ATTEMPT, assessment.warnings)
        self.assertEqual(assessment.duplicate_blocked_attempts, [])
        self.assertEqual(assessment.stale_attempts[0]["state"], "closed unmerged")

    def test_without_the_rule_the_same_attempt_is_only_stale(self) -> None:
        assessment = self._assess(
            [_open_attempt(days_since_activity=400)], forbids=False
        )

        self.assertTrue(assessment.may_start)
        self.assertIn(STALE_PRIOR_ATTEMPT, assessment.warnings)
        self.assertEqual(assessment.duplicate_blocked_attempts, [])


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
