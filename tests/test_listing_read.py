import unittest

from mailman.submission import (
    _mark_listing_read,
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


if __name__ == "__main__":
    unittest.main()
