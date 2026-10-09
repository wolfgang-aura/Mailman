import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from mailman.discover import (
    REPOSITORIES_FILE,
    build_query,
    discover,
    gh_timeline,
    query_batches,
    read_repository_list,
    render_discovery,
)
from mailman.prescreen import prescreen_path
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

    def write_screen(
        self, slug: str, verdict: str, current: bool, failed: list[str] | None = None
    ) -> None:
        path = screen_path(self.root, slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        windows = {
            "window_days": FRESHNESS_WINDOW_DAYS,
            "issue_window_days": ISSUE_WINDOW_DAYS,
            "responsiveness_days": RESPONSIVENESS_WINDOW_DAYS,
        } if current else {}
        path.write_text(json.dumps({
            "repository": slug, "success": True, "verdict": verdict,
            "failed_gates": failed or (["responsiveness"] if verdict != "pass" else []), **windows,
        }), encoding="utf-8")

    def test_the_tracked_list_reads_without_duplicates(self) -> None:
        slugs = read_repository_list(REPOSITORIES_FILE)
        self.assertGreater(len(slugs), 50)
        self.assertEqual(len(slugs), len({slug.lower() for slug in slugs}))
        self.assertTrue(all(slug.count("/") == 1 for slug in slugs))

    def test_the_option_takes_a_slug_list_as_well_as_a_file(self) -> None:
        # The help says "a slug list"; a comma list crashed as a missing file. #403
        self.assertEqual(read_repository_list("a/b, c/d,A/B"), ["a/b", "c/d"])
        with tempfile.TemporaryDirectory() as directory:
            listed = Path(directory) / "repos.txt"
            listed.write_text("e/f  # comment\n\ng/h\n", encoding="utf-8")
            self.assertEqual(read_repository_list(str(listed)), ["e/f", "g/h"])

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

    def test_a_repository_a_hunt_dropped_as_closed_is_not_searched(self) -> None:
        # streamlit was dropped as closed to outside pull requests and
        # discover offered streamlit#17234 again. Mailman #405.
        data_root = self.root / "runs"
        hunt = self.root / "hunts" / "h1" / "hunt.json"
        hunt.parent.mkdir(parents=True)
        hunt.write_text(json.dumps({"hunt_id": "h1", "runs": [{
            "run_id": "r1", "target": "a/closed#1", "dropped": True,
            "reason": "target-closed-to-outside-prs",
        }]}), encoding="utf-8")
        queries: list[str] = []
        result = discover(
            ["a/closed", "a/open"], data_root=data_root, since="2026-07-01",
            search=lambda query: queries.append(query) or [], spacing_seconds=0,
        )

        self.assertNotIn("a/closed", queries[0])
        self.assertIn("closed to outside pull requests", result["skipped"]["a/closed"])

    def test_a_repository_with_our_open_pull_request_is_not_searched(self) -> None:
        # Python-Markdown#1647 was offered while our pull request there was
        # open; the hand list of open-PR repositories did not name it. #407.
        data_root = self.root / "runs"
        hunt = self.root / "hunts" / "h1" / "hunt.json"
        hunt.parent.mkdir(parents=True)
        hunt.write_text(json.dumps({"hunt_id": "h1", "runs": [{
            "run_id": "r1", "target": "a/ours#1",
            "filed": {"pr_url": "https://github.com/a/ours/pull/2", "pr_number": 2,
                      "repository": "a/ours", "target": "a/ours#1"},
        }]}), encoding="utf-8")
        queries: list[str] = []
        result = discover(
            ["a/ours", "a/open"], data_root=data_root, since="2026-07-01",
            search=lambda query: queries.append(query) or [], spacing_seconds=0,
        )

        self.assertNotIn("a/ours", queries[0])
        self.assertIn("open pull request", result["skipped"]["a/ours"])

    def test_a_policy_refusal_under_older_windows_is_not_searched(self) -> None:
        # python/mypy refused AI-assisted work; discover listed it anyway.
        # Mailman #387.
        self.write_screen("a/policy", "fail", current=False, failed=["policy"])
        result = discover(
            ["a/policy"], data_root=self.root, since="2026-07-01",
            search=lambda query: [], spacing_seconds=0,
        )

        self.assertIn("policy", result["skipped"]["a/policy"])

    def test_an_issue_with_an_open_pull_request_on_its_timeline_is_dropped(self) -> None:
        # -linked:pr missed sqlfluff#8605 and sphinx#14666: both only
        # cross-referenced their issue. Mailman #385.
        def timeline(slug: str, number: int) -> list[dict]:
            if number != 5:
                return []
            return [{"event": "cross-referenced", "source": {"issue": {
                "html_url": f"https://github.com/{slug}/pull/6", "state": "open",
                "pull_request": {"merged_at": None},
            }}}]

        result = discover(
            ["a/one"], data_root=self.root, since="2026-07-01",
            search=lambda query: [_issue("a/one", 5, "2026-09-01"), _issue("a/one", 9, "2026-09-02")],
            timeline=timeline, spacing_seconds=0,
        )

        self.assertEqual([row["number"] for row in result["issues"]], [9])
        self.assertEqual(result["claimed"], {"a/one#5": ["a/one#6"]})
        self.assertIn("a/one#5", render_discovery(result))

    def test_only_the_freshest_hits_per_repository_read_a_timeline(self) -> None:
        # Single batches returned 97 and 100 hits; reading every timeline
        # outlasted a 50-minute budget. Mailman #389.
        read: list[int] = []

        def timeline(slug: str, number: int) -> list[dict]:
            read.append(number)
            return []

        hits = [_issue("a/big", n, f"2026-09-{n:02d}") for n in range(1, 11)]
        result = discover(
            ["a/big"], data_root=self.root, since="2026-07-01",
            search=lambda query: hits, timeline=timeline,
            per_repository=3, spacing_seconds=0,
        )

        self.assertEqual(sorted(read), [8, 9, 10])
        self.assertEqual([row["number"] for row in result["issues"]], [10, 9, 8])

    def test_an_issue_claimed_in_a_comment_is_dropped(self) -> None:
        # streamlit#17216 and deepagents#6401 were listed, then refused by
        # prescreen as unacknowledged-claim. Mailman #396.
        def timeline(slug: str, number: int) -> list[dict]:
            if number == 5:
                return [{"event": "commented", "author_association": "NONE",
                         "user": {"login": "someone", "type": "User"},
                         "created_at": datetime.now(UTC).isoformat(),
                         "body": "I'd like to work on this, can I take it?"}]
            if number == 7:
                return [{"event": "commented", "author_association": "NONE",
                         "user": {"login": "old", "type": "User"},
                         "created_at": "2025-01-01T00:00:00Z",
                         "body": "I'll work on this."}]
            return []

        result = discover(
            ["a/one"], data_root=self.root, since="2026-07-01",
            search=lambda query: [_issue("a/one", 5, "2026-09-01"), _issue("a/one", 7, "2026-09-02")],
            timeline=timeline, spacing_seconds=0,
        )

        self.assertEqual([row["number"] for row in result["issues"]], [7])
        self.assertEqual(result["claimed"], {"a/one#5": ["comment by @someone"]})

    def test_a_reporter_offering_a_fix_in_the_report_has_claimed_it(self) -> None:
        # streamlit#17216's only claim was in the report. Mailman #396.
        offered = _issue("a/one", 5, "2026-09-01") | {
            "author_association": "NONE",
            "user": {"login": "reporter", "type": "User"},
            "created_at": datetime.now(UTC).isoformat(),
            "body": "Steps to reproduce below. I'm happy to submit a PR for this.",
        }
        result = discover(
            ["a/one"], data_root=self.root, since="2026-07-01",
            search=lambda query: [offered, _issue("a/one", 9, "2026-09-02")],
            timeline=lambda slug, number: [], spacing_seconds=0,
        )

        self.assertEqual([row["number"] for row in result["issues"]], [9])
        self.assertEqual(result["claimed"], {"a/one#5": ["comment by @reporter"]})

    def test_an_unread_timeline_is_reported_not_counted_as_unclaimed(self) -> None:
        # A failed timeline read found no rivals and listed the issue as a
        # candidate. Mailman #390.
        result = discover(
            ["a/one"], data_root=self.root, since="2026-07-01",
            search=lambda query: [_issue("a/one", 5, "2026-09-01"), _issue("a/one", 9, "2026-09-02")],
            timeline=lambda slug, number: None if number == 5 else [], spacing_seconds=0,
        )

        self.assertEqual([row["number"] for row in result["issues"]], [9])
        self.assertEqual(result["unread_timelines"], ["a/one#5"])
        self.assertIn("TIMELINE NOT READ", render_discovery(result))

    def test_a_failed_timeline_read_is_retried_and_its_reason_reported(self) -> None:
        # 2026-10-09: 981 of 1014 timelines came back unread with no reason,
        # and every one of them read fine an hour later. Mailman #478.
        class _Done:
            def __init__(self, stdout, returncode=0, stderr=""):
                self.stdout, self.returncode, self.stderr = stdout, returncode, stderr

        calls = []

        def run(arguments, **keywords):
            calls.append(arguments[-1])
            if "/5/" in arguments[-1]:
                return _Done("", 1, "error connecting to api.github.com")
            if len(calls) == 2:
                return _Done("[]")
            return _Done("", 1, "HTTP 502")

        timeline = gh_timeline(_run=run, _sleep=lambda seconds: None)
        result = discover(
            ["a/one"], data_root=self.root, since="2026-07-01",
            search=lambda query: [_issue("a/one", 5, "2026-09-01"), _issue("a/one", 9, "2026-09-02")],
            timeline=timeline, spacing_seconds=0,
        )

        self.assertEqual([row["number"] for row in result["issues"]], [9])
        self.assertEqual(result["unread_timelines"], ["a/one#5"])
        self.assertEqual(len(calls), 4)
        self.assertEqual(result["timeline_errors"], ["a/one#5: exit 1: error connecting to api.github.com"])
        self.assertIn("because a/one#5: exit 1: error connecting", render_discovery(result))

    def test_a_maintainer_triaged_report_ranks_first_and_is_marked(self) -> None:
        # A hunt counts only triaged runs; the coordinator filtered a
        # 180-day list for them by hand. Mailman #393.
        fresh = _issue("a/one", 9, "2026-09-20") | {"author_association": "NONE"}
        commented = _issue("a/one", 5, "2026-09-01") | {"author_association": "NONE"}
        labelled = _issue("a/one", 4, "2026-08-30") | {"author_association": "NONE"}
        by_bot = _issue("a/one", 3, "2026-08-29") | {"author_association": "NONE"}
        filed = _issue("a/one", 2, "2026-08-28")

        def timeline(slug: str, number: int) -> list[dict]:
            return {
                5: [{"event": "commented", "author_association": "MEMBER"}],
                4: [{"event": "labeled", "actor": {"login": "keeper", "type": "User"},
                     "label": {"name": "P2"}}],
                3: [{"event": "labeled", "actor": {"login": "triage[bot]", "type": "Bot"},
                     "label": {"name": "bug"}}],
            }.get(number, [])

        result = discover(
            ["a/one"], data_root=self.root, since="2026-07-01",
            search=lambda query: [fresh, commented, labelled, by_bot, filed],
            timeline=timeline, per_repository=10, spacing_seconds=0,
        )

        self.assertEqual([row["number"] for row in result["issues"]], [5, 4, 2, 9, 3])
        self.assertEqual([row["engaged"] for row in result["issues"]],
                         [True, True, True, False, False])
        self.assertIn("3 triaged", render_discovery(result))

    def test_an_issue_prescreen_rejected_is_dropped_before_its_timeline(self) -> None:
        # Five of 14 triaged rows on 2026-10-02 were copier issues prescreen
        # had already rejected. Mailman #394.
        record = prescreen_path(self.root, "a/one", 5)
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({
            "verdict": "reject", "blocking": ["maintainer-disputed"],
        }), encoding="utf-8")
        read: list[int] = []

        def timeline(slug: str, number: int) -> list[dict]:
            read.append(number)
            return []

        result = discover(
            ["a/one"], data_root=self.root, since="2026-07-01",
            search=lambda query: [_issue("a/one", 5, "2026-09-01"), _issue("a/one", 9, "2026-09-02")],
            timeline=timeline, spacing_seconds=0,
        )

        self.assertEqual(read, [9])
        self.assertEqual([row["number"] for row in result["issues"]], [9])
        self.assertEqual(result["rejected"], {"a/one#5": ["maintainer-disputed"]})
        self.assertIn("rejected a/one#5", render_discovery(result))

    def test_a_rerun_reuses_answered_batches_from_the_cache(self) -> None:
        # A 126-batch pass was killed at batch 94 and kept nothing. #395.
        cache = self.root / "discover-cache"
        first = discover(
            ["a/one"], data_root=self.root, since="2026-07-01",
            search=lambda query: [_issue("a/one", 9, "2026-09-02")],
            spacing_seconds=0, cache_directory=cache,
        )
        again = discover(
            ["a/one"], data_root=self.root, since="2026-07-01",
            search=lambda query: None, spacing_seconds=0, cache_directory=cache,
        )

        self.assertEqual(again["issues"], first["issues"])
        self.assertEqual(again["unsearched"], [])

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
