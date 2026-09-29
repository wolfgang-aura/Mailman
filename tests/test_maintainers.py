"""Maintainers GitHub reports as CONTRIBUTOR. Mailman #203.

marimo's mscolnick has a private org membership, so his parked fix
marimo-team/marimo#9862 read as an outsider's attempt. The repository screen
now records who merged recent pull requests, and every maintainer check
accepts that set beside the association.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.maintainers import (
    MERGERS_QUERY,
    is_maintainer,
    load_maintainer_logins,
    maintainer_logins,
    mergers,
)
from mailman.prior_art import closes_issue


def _merged(*logins: tuple[str, str]) -> dict:
    """A GraphQL answer to MERGERS_QUERY, one node per (login, __typename)."""
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": [
                        {"mergedBy": {"login": login, "__typename": kind}}
                        for login, kind in logins
                    ]
                    + [{"mergedBy": None}]
                }
            }
        }
    }


class IsMaintainerTests(unittest.TestCase):
    def test_an_association_alone_still_decides(self) -> None:
        for association in ("OWNER", "MEMBER", "COLLABORATOR"):
            with self.subTest(association=association):
                self.assertTrue(
                    is_maintainer({"author_association": association})
                )
        self.assertFalse(is_maintainer({"author_association": "CONTRIBUTOR"}))

    def test_a_listed_login_counts_whatever_the_association(self) -> None:
        comment = {
            "author_association": "CONTRIBUTOR",
            "user": {"login": "mscolnick"},
        }
        self.assertTrue(is_maintainer(comment, {"MScolnick"}))
        self.assertFalse(is_maintainer(comment, {"akshayka"}))

    def test_no_set_answers_exactly_as_the_association_did(self) -> None:
        comment = {"author_association": "CONTRIBUTOR", "user": {"login": "x"}}
        self.assertFalse(is_maintainer(comment))
        self.assertFalse(is_maintainer(comment, frozenset()))

    def test_every_row_shape_names_its_author(self) -> None:
        listed = {"mscolnick"}
        for row in (
            {"user": {"login": "mscolnick"}},
            {"author": {"login": "mscolnick"}, "authorAssociation": "NONE"},
            {"author": "mscolnick", "association": "CONTRIBUTOR"},
            {"login": "mscolnick"},
        ):
            with self.subTest(row=row):
                self.assertTrue(is_maintainer(row, listed))

    def test_a_narrower_association_set_is_honoured(self) -> None:
        row = {"author_association": "COLLABORATOR", "user": {"login": "c"}}
        self.assertFalse(
            is_maintainer(row, associations={"OWNER", "MEMBER"})
        )
        self.assertTrue(
            is_maintainer(row, {"c"}, associations={"OWNER", "MEMBER"})
        )

    def test_a_non_dict_is_never_a_maintainer(self) -> None:
        self.assertFalse(is_maintainer(None, {"x"}))
        self.assertFalse(is_maintainer("x", {"x"}))


class MergersTests(unittest.TestCase):
    def test_the_humans_who_merged_are_the_set(self) -> None:
        answer = _merged(
            ("mscolnick", "User"),
            ("akshayka", "User"),
            ("mscolnick", "User"),
            ("renovate", "Bot"),
            ("github-actions[bot]", "User"),
            ("dependabot-preview", "User"),
        )
        self.assertEqual(mergers(answer), ["akshayka", "mscolnick"])

    def test_a_failed_or_odd_answer_is_no_set(self) -> None:
        for answer in (
            None,
            {},
            {"errors": [{"message": "rate limited"}]},
            {"data": {"repository": None}},
            {"data": {"repository": {"pullRequests": {"nodes": "x"}}}},
        ):
            with self.subTest(answer=answer):
                self.assertEqual(mergers(answer), [])

    def test_the_query_names_the_repository_and_one_page(self) -> None:
        query = MERGERS_QUERY % ("marimo-team", "marimo")
        self.assertIn('owner: "marimo-team", name: "marimo"', query)
        self.assertIn("states: MERGED, last: 100", query)
        self.assertIn("mergedBy { login __typename }", query)


class StoredSetTests(unittest.TestCase):
    def test_a_screen_without_the_field_has_no_set(self) -> None:
        self.assertEqual(maintainer_logins(None), frozenset())
        self.assertEqual(maintainer_logins({}), frozenset())
        self.assertEqual(maintainer_logins({"maintainer_logins": "x"}), frozenset())
        self.assertEqual(
            maintainer_logins({"maintainer_logins": ["a", "", None, "b"]}),
            frozenset({"a", "b"}),
        )

    def test_the_set_loads_from_the_stored_screen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(
                load_maintainer_logins(root, "marimo-team/marimo"), frozenset()
            )
            screens = root / "screens"
            screens.mkdir()
            (screens / "marimo-team__marimo.json").write_text(
                json.dumps(
                    {
                        "repository": "marimo-team/marimo",
                        "maintainer_logins": ["mscolnick"],
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                load_maintainer_logins(root, "marimo-team/marimo"),
                frozenset({"mscolnick"}),
            )
        self.assertEqual(load_maintainer_logins(None, "a/b"), frozenset())
        self.assertEqual(load_maintainer_logins(Path("."), ""), frozenset())


class ClosesIssueTests(unittest.TestCase):
    def test_the_closing_forms_count(self) -> None:
        for body in (
            "Fixes #9808",
            "fixes: #9808",
            "Closes marimo-team/marimo#9808",
            "Resolves https://github.com/marimo-team/marimo/issues/9808",
            "Some text.\n\nFixed #9808 and more.",
        ):
            with self.subTest(body=body):
                self.assertTrue(
                    closes_issue(body, repository="marimo-team/marimo", number=9808)
                )

    def test_a_mention_or_another_issue_does_not(self) -> None:
        for body in (
            "Related to #9808",
            "Fixes #98080",
            "Fixes #980",
            "Fixes other/repo#9808",
            "Fixes https://github.com/marimo-team/marimo/issues/9808/x",
            "",
            None,
        ):
            with self.subTest(body=body):
                self.assertFalse(
                    closes_issue(body, repository="marimo-team/marimo", number=9808)
                )
        self.assertFalse(
            closes_issue("Fixes #1", repository="a/b", number=None)
        )


if __name__ == "__main__":
    unittest.main()
