import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mailman.executor import CommandResult
from mailman.submission import (
    _mark_listing_read,
    _read_unlisted_rows,
    _match_rows,
    duplicate_is_related,
    related_duplicates,
)


def _index_row(number: int) -> dict:
    entry = {"number": number, "title": "Minimal Viable OpenGLView", "state": "open"}
    row = _match_rows([entry], pull_request=True, method="search")[0]
    row["methods"].append("list")
    return row


class ListingReadTests(unittest.TestCase):
    """beeware/toga#4279 was an index hit on `startup exception`, probably from
    a comment. The open-PR listing read its title and body, matched neither
    term, and so emitted no row; the index hit then stood as a live rival that
    "claims" toga#3628. #243."""

    def test_an_index_hit_the_listing_read_and_did_not_match_is_not_related(self) -> None:
        row = _index_row(4279)
        record = {"matches": [row]}
        listing = [{"number": 4279, "title": "Minimal Viable OpenGLView", "body": "GL"}]
        _mark_listing_read(
            record, listing, rows=[], pull_request=True, term_count=2
        )
        self.assertTrue(row["listing_read"])
        self.assertFalse(duplicate_is_related(row))
        self.assertEqual(related_duplicates(record["matches"], issue_number=3628), [])

    def test_an_index_hit_the_listing_never_saw_still_stands(self) -> None:
        row = _index_row(4279)
        record = {"matches": [row]}
        _mark_listing_read(record, [], rows=[], pull_request=True, term_count=2)
        self.assertFalse(row.get("listing_read"))
        self.assertTrue(duplicate_is_related(row))

    def test_an_index_hit_that_cites_the_issue_still_stands(self) -> None:
        row = _index_row(4279)
        row["references_issue"] = True
        record = {"matches": [row]}
        listing = [{"number": 4279, "title": "x", "body": "y"}]
        _mark_listing_read(record, listing, rows=[], pull_request=True, term_count=2)
        self.assertTrue(duplicate_is_related(row))

    def test_an_issue_listing_does_not_mark_a_pull_request_row(self) -> None:
        row = _index_row(4279)
        record = {"matches": [row]}
        listing = [{"number": 4279, "title": "x", "body": "y"}]
        _mark_listing_read(record, listing, rows=[], pull_request=False, term_count=2)
        self.assertTrue(duplicate_is_related(row))


def _result(stdout: str, exit_code: int = 0) -> CommandResult:
    return CommandResult(
        command=["gh"], working_directory=".", started_at="", duration_seconds=0.0,
        exit_code=exit_code, stdout=stdout, stderr="", timed_out=False,
        timeout_seconds=60, environment={},
    )


class UnlistedRowTests(unittest.TestCase):
    """spack pr#48947, "Override package directives", was an index-only open
    hit the listing never reached among spack's open pull requests. It stood
    as a rival with no way to clear it. Its own title and body are read. #292."""

    def _read(self, view: dict | None, exit_code: int = 0) -> tuple[dict, list]:
        row = _index_row(48947)
        record = {"matches": [row], "commands": []}
        calls: list = []

        def fake(command, **_kwargs):
            calls.append(command)
            return _result(json.dumps(view) if view else "", exit_code)

        with tempfile.TemporaryDirectory() as directory, patch(
            "mailman.submission.execute", fake
        ):
            _read_unlisted_rows(
                record, Path(directory), slug="spack/spack", executable="gh",
                query="resource directive package hash", issue_number=52662,
                timeout_seconds=60,
            )
        return row, calls

    def test_a_partial_match_in_its_own_text_is_not_related(self) -> None:
        row, calls = self._read(
            {"number": 48947, "title": "Override package directives",
             "body": "Lets a repo override directives.", "headRefName": "x"}
        )
        self.assertEqual(calls[0][:3], ["gh", "pr", "view"])
        self.assertFalse(duplicate_is_related(row))

    def test_a_full_match_in_its_own_text_still_stands(self) -> None:
        row, _ = self._read(
            {"number": 48947, "title": "Hash resource directives",
             "body": "Add each resource directive to the package hash.",
             "headRefName": "x"}
        )
        self.assertTrue(duplicate_is_related(row))

    def test_a_failed_read_leaves_the_row_standing(self) -> None:
        row, _ = self._read(None, exit_code=1)
        self.assertTrue(duplicate_is_related(row))

    def test_a_row_the_listing_already_read_is_not_fetched_again(self) -> None:
        row = _index_row(48947)
        row["listing_read"] = True
        record = {"matches": [row], "commands": []}
        with tempfile.TemporaryDirectory() as directory, patch(
            "mailman.submission.execute", side_effect=AssertionError
        ):
            _read_unlisted_rows(
                record, Path(directory), slug="spack/spack", executable="gh",
                query="resource directive", issue_number=1, timeout_seconds=60,
            )


if __name__ == "__main__":
    unittest.main()
