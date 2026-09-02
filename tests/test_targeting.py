from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.targeting import (
    NO_DUPLICATE_SEARCH,
    OPEN_PULL_REQUEST,
    UNACKNOWLEDGED_ATTEMPTS,
    assess_target,
)


def _record(root: Path, *, searched: bool = True, attempts: list | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "duplicate-search.json").write_text(
        json.dumps({"success": searched, "matches": []}), encoding="utf-8"
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

    def test_a_malformed_record_is_treated_as_no_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "duplicate-search.json").write_text("{not json", encoding="utf-8")

            assessment = assess_target(root)

        self.assertIn(NO_DUPLICATE_SEARCH, assessment.blocking)


if __name__ == "__main__":
    unittest.main()
