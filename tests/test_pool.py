import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from mailman.pool import build_pool, render_pool, repository_from_urls
from mailman.screen import SCREENS_DIRECTORY

NOW = datetime(2026, 10, 3, tzinfo=UTC)


def _node(slug: str, *, outside: int, stars: int = 2000, language: str = "Python") -> dict:
    merged = [
        {"mergedAt": "2026-09-25T00:00:00Z", "authorAssociation": "CONTRIBUTOR",
         "author": {"login": f"person{index}"}}
        for index in range(outside)
    ]
    # Never counted: a bot, a member, and an outside merge older than the window.
    merged += [
        {"mergedAt": "2026-09-25T00:00:00Z", "authorAssociation": "NONE",
         "author": {"login": "dependabot[bot]"}},
        {"mergedAt": "2026-09-25T00:00:00Z", "authorAssociation": "MEMBER",
         "author": {"login": "maintainer"}},
        {"mergedAt": "2026-06-01T00:00:00Z", "authorAssociation": "CONTRIBUTOR",
         "author": {"login": "longago"}},
    ]
    return {
        "nameWithOwner": slug, "stargazerCount": stars, "isArchived": False,
        "isFork": False, "primaryLanguage": {"name": language},
        "issues": {"totalCount": 50}, "pullRequests": {"nodes": merged},
    }


class PoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        screens = self.root / SCREENS_DIRECTORY
        screens.mkdir()
        (screens / "a__screened.json").write_text(
            json.dumps({"repository": "a/screened"}), encoding="utf-8"
        )
        self.urls = {
            "busy": ["https://github.com/a/busy"],
            "quiet": ["https://github.com/a/quiet"],
            "screened": ["https://github.com/a/screened"],
            "rust": ["https://github.com/a/rust"],
            "nolink": ["https://example.org"],
        }
        self.nodes = {
            "a/busy": _node("a/busy", outside=4),
            "a/quiet": _node("a/quiet", outside=1),
            "a/rust": _node("a/rust", outside=5, language="Rust"),
        }
        self.queries: list[str] = []

    def graphql(self, query: str) -> dict:
        self.queries.append(query)
        answer = {}
        for index in range(40):
            for slug, node in self.nodes.items():
                owner, name = slug.split("/")
                if f'r{index}: repository(owner: "{owner}", name: "{name}")' in query:
                    answer[f"r{index}"] = node
        return answer

    def test_unscreened_repositories_merging_outside_work_are_kept(self) -> None:
        result = build_pool(
            data_root=self.root,
            top_packages=lambda top: list(self.urls)[:top],
            project_urls=self.urls.get,
            graphql=self.graphql,
            now=NOW,
            workers=1,
        )

        self.assertEqual([row["repository"] for row in result["pool"]], ["a/busy"])
        self.assertEqual(result["pool"][0]["outside_authors"], 4)
        self.assertEqual(result["unread"], [])
        self.assertNotIn("a/screened", "".join(self.queries))
        self.assertIn("a/busy", render_pool(result))

    def test_an_unread_package_or_batch_is_reported_not_dropped(self) -> None:
        self.urls["broken"] = None
        result = build_pool(
            data_root=self.root,
            top_packages=lambda top: list(self.urls),
            project_urls=self.urls.get,
            graphql=lambda query: None,
            now=NOW,
            workers=1,
        )

        self.assertIn("broken", result["unread"])
        self.assertIn("graphql batch 1", result["unread"])
        self.assertIn("UNREAD", render_pool(result))

    def test_an_unreadable_top_list_is_not_an_empty_pool(self) -> None:
        result = build_pool(
            data_root=self.root, top_packages=lambda top: None,
            project_urls=self.urls.get, graphql=self.graphql, now=NOW,
        )

        self.assertEqual(result["unread"], ["top packages"])

    def test_a_sponsor_link_is_not_the_repository(self) -> None:
        self.assertEqual(
            repository_from_urls(["https://github.com/sponsors/x", "https://github.com/x/lib.git"]),
            "x/lib",
        )


if __name__ == "__main__":
    unittest.main()
