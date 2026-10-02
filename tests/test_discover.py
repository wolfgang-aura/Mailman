import json
import tempfile
import unittest
from pathlib import Path

from mailman.discover import (
    REPOSITORIES_FILE,
    build_query,
    discover,
    query_batches,
    read_repository_list,
    render_discovery,
)
from mailman.screen import (
    FRESHNESS_WINDOW_DAYS,
    ISSUE_WINDOW_DAYS,
    RESPONSIVENESS_WINDOW_DAYS,
    screen_path,
)


def _issue(slug: str, number: int, created: str) -> dict:
    return {
        "repository_url": f"https://api.github.com/repos/{slug}",
        "number": number,
        "title": f"bug {number}",
        "created_at": f"{created}T00:00:00Z",
        "comments": 1,
        "author_association": "MEMBER",
        "labels": [{"name": "bug"}],
    }


class DiscoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_screen(self, slug: str, verdict: str, current: bool) -> None:
        path = screen_path(self.root, slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        windows = {
            "window_days": FRESHNESS_WINDOW_DAYS,
            "issue_window_days": ISSUE_WINDOW_DAYS,
            "responsiveness_days": RESPONSIVENESS_WINDOW_DAYS,
        } if current else {}
        path.write_text(json.dumps({
            "repository": slug, "success": True, "verdict": verdict,
            "failed_gates": ["responsiveness"] if verdict != "pass" else [], **windows,
        }), encoding="utf-8")

    def test_the_tracked_list_reads_without_duplicates(self) -> None:
        slugs = read_repository_list(REPOSITORIES_FILE)
        self.assertGreater(len(slugs), 50)
        self.assertEqual(len(slugs), len({slug.lower() for slug in slugs}))
        self.assertTrue(all(slug.count("/") == 1 for slug in slugs))

    def test_batches_fit_the_query_limit(self) -> None:
        slugs = [f"owner{index}/repository{index}" for index in range(30)]
        batches = query_batches(slugs)
        self.assertEqual([slug for batch in batches for slug in batch], slugs)
        for batch in batches:
            self.assertLessEqual(len(" ".join(f"repo:{slug}" for slug in batch)), 170)
        self.assertIn("-linked:pr", build_query(batches[0], "2026-07-01"))

    def test_a_current_refusal_and_an_exclusion_are_not_searched(self) -> None:
        # sqlalchemy, twine and sqlite-utils were re-screened on 2026-10-02
        # and refused again on the gate already on disk. Mailman #384.
        self.write_screen("a/refused", "fail", current=True)
        self.write_screen("a/stale", "fail", current=False)
        queries: list[str] = []

        def search(query: str) -> list[dict]:
            queries.append(query)
            return [_issue("a/stale", 5, "2026-09-01"), _issue("a/fresh", 9, "2026-09-20")]

        result = discover(
            ["a/refused", "a/stale", "a/fresh", "a/ours"],
            data_root=self.root, since="2026-07-01", search=search,
            excluded=["A/Ours"], spacing_seconds=0,
        )

        self.assertEqual(len(queries), 1)
        self.assertNotIn("a/refused", queries[0])
        self.assertNotIn("a/ours", queries[0])
        self.assertIn("repo:a/stale", queries[0])
        self.assertEqual(result["skipped"]["a/ours"], "excluded")
        self.assertIn("responsiveness", result["skipped"]["a/refused"])
        self.assertEqual([row["number"] for row in result["issues"]], [9, 5])

    def test_an_unanswered_batch_is_reported_not_counted_as_empty(self) -> None:
        # The scratch search printed "0 from 7 repos" for a batch it could
        # not reach. Mailman #384.
        result = discover(
            ["a/one", "a/two"], data_root=self.root, since="2026-07-01",
            search=lambda query: None, spacing_seconds=0,
        )

        self.assertEqual(result["unsearched"], ["a/one", "a/two"])
        self.assertEqual(result["searched"], 0)
        self.assertIn("NOT SEARCHED", render_discovery(result))


if __name__ == "__main__":
    unittest.main()
