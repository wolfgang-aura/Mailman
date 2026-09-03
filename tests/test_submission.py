from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mailman.models import AgentConfig, RunRecord, RunStatus
from mailman.submission import (
    TargetPolicy,
    _local_matches,
    _match_rows,
    _query_terms,
    analyze_diff,
    duplicate_is_related,
    duplicate_strength,
    partition_duplicates,
    related_duplicates,
    prepare_submission,
    record_duplicate_acknowledgement,
    record_no_test_acknowledgement,
)


def _weak_match(number: int) -> dict[str, object]:
    """A listing hit on one common word out of four. Not a duplicate."""
    return {
        "number": number,
        "title": "ENH: Add exit tags via Trade.close(tag=)",
        "state": "OPEN",
        "pull_request": True,
        "matched_by": ["price"],
        "methods": ["listing"],
        "matched_terms": ["price"],
        "term_count": 4,
        "references_issue": False,
    }


LISTING = [
    {
        "number": 3485,
        "title": "Delay background task execution until BaseHTTPMiddleware sends it",
        "body": "Fixes #3458.",
        "headRefName": "fix/background-task-timing",
        "state": "OPEN",
    },
    {
        "number": 3476,
        "title": "fix: defer inner response background task until response is sent",
        "body": "The middleware buffers the response.",
        "headRefName": "patch-1",
        "state": "OPEN",
    },
    {
        "number": 3151,
        "title": "Add max_body_size parameter",
        "body": "Unrelated work.",
        "headRefName": "max-size2",
        "state": "OPEN",
    },
]


SOURCE_DIFF = """diff --git a/src/thing.py b/src/thing.py
index 1111111..2222222 100644
--- a/src/thing.py
+++ b/src/thing.py
@@ -1,3 +1,3 @@
 def thing(value):
-    return value
+    return value.strip()
diff --git a/tests/test_thing.py b/tests/test_thing.py
index 3333333..4444444 100644
--- a/tests/test_thing.py
+++ b/tests/test_thing.py
@@ -1,2 +1,4 @@
 from src.thing import thing
+def test_strips():
+    assert thing(" a ") == "a"
"""

NEWLINE_ONLY_DIFF = """diff --git a/docs/notes.md b/docs/notes.md
index 5555555..6666666 100644
--- a/docs/notes.md
+++ b/docs/notes.md
@@ -1 +1 @@
-a line
\\ No newline at end of file
+a line
"""


def _policy(**overrides: object) -> TargetPolicy:
    payload: dict[str, object] = {
        "name": "example/project",
        "policy_url": "https://example.invalid/policy",
        "stance": "permitted",
        "policy_read_on": "2026-09-02",
    }
    payload.update(overrides)
    return TargetPolicy.from_dict(payload)


def _run() -> RunRecord:
    return RunRecord(
        run_id="20260902T000000Z-abcdef",
        repository="https://github.com/example/project.git",
        issue="https://github.com/example/project/issues/7",
        base_commit="a" * 40,
        primary=AgentConfig(agent="codex"),
        reviewer=AgentConfig(agent="claude"),
        status=RunStatus.READY_FOR_HUMAN_REVIEW,
    )


class AnalyzeDiffTests(unittest.TestCase):
    def test_a_source_and_test_change_is_clean(self) -> None:
        report = analyze_diff(SOURCE_DIFF)
        self.assertFalse(report["blocking"])
        self.assertEqual(
            [entry["path"] for entry in report["files"]],
            ["src/thing.py", "tests/test_thing.py"],
        )
        self.assertTrue(report["files"][1]["test"])

    def test_a_trailing_newline_change_blocks(self) -> None:
        report = analyze_diff(NEWLINE_ONLY_DIFF)
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("newline-only-change", codes)
        self.assertTrue(report["blocking"])

    def test_a_source_change_with_no_test_blocks(self) -> None:
        source_only = SOURCE_DIFF.split("diff --git a/tests")[0]
        report = analyze_diff(source_only)
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("no-test-change", codes)

    def test_an_acknowledgement_keeps_the_finding_but_stops_it_blocking(self) -> None:
        source_only = SOURCE_DIFF.split("diff --git a/tests")[0]
        report = analyze_diff(
            source_only,
            no_test_acknowledgement={"covered_paths": ["src/thing.py"]},
        )
        finding = next(
            entry
            for entry in report["findings"]
            if entry["code"] == "no-test-change"
        )
        self.assertFalse(finding["blocking"])
        self.assertFalse(report["blocking"])
        self.assertIn("no-test-acknowledgement.json", finding["detail"])

    def test_an_acknowledgement_does_not_cover_a_different_path(self) -> None:
        source_only = SOURCE_DIFF.split("diff --git a/tests")[0]
        report = analyze_diff(
            source_only,
            no_test_acknowledgement={"covered_paths": ["src/other.py"]},
        )
        self.assertTrue(report["blocking"])

    def test_a_source_path_containing_test_is_not_a_test_file(self) -> None:
        # src/_pytest/raises.py is production code in pytest's own tree. A
        # substring match called it a test and hid a change with no coverage.
        diff = SOURCE_DIFF.replace("src/thing.py", "src/_pytest/raises.py").split(
            "diff --git a/tests"
        )[0]
        report = analyze_diff(diff)
        self.assertFalse(report["files"][0]["test"])
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("no-test-change", codes)

    def test_a_testing_directory_counts_as_coverage(self) -> None:
        diff = SOURCE_DIFF.replace("tests/test_thing.py", "testing/python/raises_group.py")
        report = analyze_diff(diff)
        self.assertTrue(report["files"][1]["test"])
        self.assertFalse(report["blocking"])

    def test_an_empty_diff_blocks(self) -> None:
        report = analyze_diff("")
        codes = {finding["code"] for finding in report["findings"]}
        self.assertEqual(codes, {"empty-diff"})


class TargetPolicyTests(unittest.TestCase):
    def test_an_unknown_stance_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _policy(stance="whatever")

    def test_a_required_trailer_needs_a_form(self) -> None:
        with self.assertRaises(ValueError):
            _policy(ai_trailer="required")

    def test_unknown_fields_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _policy(mystery=True)

    def test_the_shipped_policies_load(self) -> None:
        directory = Path(__file__).resolve().parent.parent / "examples" / "target-policies"
        names = sorted(path.name for path in directory.glob("*.json"))
        self.assertEqual(
            names, ["attrs.json", "langchain.json", "pytest.json", "starlette.json"]
        )
        for path in directory.glob("*.json"):
            policy = TargetPolicy.load(path)
            self.assertTrue(policy.policy_read_on, path.name)


class DuplicateSearchRowTests(unittest.TestCase):
    def test_rows_carry_the_kind_the_caller_searched(self) -> None:
        payload = [
            {
                "number": 14358,
                "title": "Fix RaisesGroup calling check() on contained exceptions",
                "state": "CLOSED",
                "url": "https://github.com/pytest-dev/pytest/pull/14358",
                "createdAt": "2026-04-06T00:00:00Z",
            }
        ]
        rows = _match_rows(payload, pull_request=True)
        self.assertEqual(rows[0]["number"], 14358)
        self.assertTrue(rows[0]["pull_request"])
        self.assertFalse(_match_rows(payload, pull_request=False)[0]["pull_request"])

    def test_an_unexpected_payload_yields_no_rows(self) -> None:
        self.assertEqual(_match_rows({"unexpected": True}, pull_request=True), [])
        self.assertEqual(_match_rows(["not a dict"], pull_request=True), [])


class LocalMatchTests(unittest.TestCase):
    def test_short_words_are_not_search_terms(self) -> None:
        self.assertEqual(_query_terms("a of BaseHTTPMiddleware"), ["basehttpmiddleware"])

    def test_a_title_term_matches(self) -> None:
        rows = _local_matches(
            LISTING, pull_request=True, query="BaseHTTPMiddleware", issue_number=None
        )
        self.assertEqual([row["number"] for row in rows], [3485])

    def test_the_issue_number_matches_a_body_or_branch(self) -> None:
        # The one the query terms miss: its title and body name neither token.
        rows = _local_matches(
            LISTING, pull_request=True, query="nothingmatches", issue_number=3458
        )
        self.assertEqual([row["number"] for row in rows], [3485])
        self.assertEqual(rows[0]["matched_by"], ["#3458"])

    def test_an_unrelated_entry_does_not_match(self) -> None:
        rows = _local_matches(
            LISTING, pull_request=True, query="BackgroundTask", issue_number=3458
        )
        self.assertNotIn(3151, [row["number"] for row in rows])

    def test_a_partial_number_does_not_match(self) -> None:
        self.assertEqual(
            _local_matches(
                LISTING, pull_request=True, query="nothingmatches", issue_number=345
            ),
            [],
        )

    def test_an_issue_needs_more_than_one_common_word(self) -> None:
        # Issue #31: "price" alone dragged eighteen unrelated issues into the
        # gate on kernc/backtesting.py.
        listing = [
            {"number": 1405, "title": "Where do I forward-test this?", "body": "price"}
        ]
        self.assertEqual(
            _local_matches(
                listing,
                pull_request=False,
                query="forced liquidation margin price",
                issue_number=939,
                minimum_terms=2,
            ),
            [],
        )
        rows = _local_matches(
            listing,
            pull_request=True,
            query="forced liquidation margin price",
            issue_number=939,
            minimum_terms=1,
        )
        self.assertEqual([row["number"] for row in rows], [1405])
        self.assertEqual(rows[0]["matched_terms"], ["price"])
        self.assertEqual(rows[0]["term_count"], 4)

    def test_the_listing_never_returns_the_run_own_issue(self) -> None:
        listing = [{"number": 939, "title": "forced liquidation", "body": ""}]
        self.assertEqual(
            _local_matches(
                listing,
                pull_request=False,
                query="forced liquidation",
                issue_number=939,
            ),
            [],
        )

    def test_a_closed_attempt_is_prior_art_not_a_duplicate(self) -> None:
        # Issue #32: langchain's bot closed PR #39682 on the day it opened, for
        # an unrelated reason. It never blocks a run outright.
        closed = dict(_weak_match(39682), state="closed", methods=["search"])
        strong, weak = partition_duplicates([closed])
        self.assertEqual(strong, [])
        self.assertEqual([row["number"] for row in weak], [39682])

    def test_a_merged_pull_request_says_the_fix_already_landed(self) -> None:
        merged = dict(_weak_match(1), state="merged", methods=["search"])
        strong, _ = partition_duplicates([merged])
        self.assertEqual([row["number"] for row in strong], [1])

    def test_a_closed_attempt_is_still_relevant_prior_art(self) -> None:
        # It must not block the gate and must still reach the prompts.
        closed = dict(_weak_match(39682), state="closed", methods=["search"])
        self.assertTrue(duplicate_is_related(closed))
        self.assertEqual([row["number"] for row in related_duplicates([closed])], [39682])
        self.assertEqual(partition_duplicates([closed])[0], [])

    def test_a_merged_row_on_one_shared_word_is_not_an_upstream_fix(self) -> None:
        stray = dict(_weak_match(1), state="merged")
        self.assertEqual(duplicate_strength(stray), "weak")

    def test_partition_splits_index_hits_from_listing_noise(self) -> None:
        index_hit = dict(_weak_match(1), methods=["search"])
        strong, weak = partition_duplicates([index_hit, _weak_match(2)])
        self.assertEqual([row["number"] for row in strong], [1])
        self.assertEqual([row["number"] for row in weak], [2])


    def test_a_non_list_payload_is_no_match(self) -> None:
        self.assertEqual(
            _local_matches(
                {"not": "a list"}, pull_request=True, query="x", issue_number=1
            ),
            [],
        )


class PrepareSubmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.run_directory = Path(self._temporary.name)
        (self.run_directory / "verification.json").write_text(
            json.dumps(
                [{"command": ["python", "-m", "pytest", "-q"], "exit_code": 0}]
            ),
            encoding="utf-8",
        )
        (self.run_directory / "issue.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "title": "thing does not strip",
                    "reference": {"owner": "example", "repository": "project", "number": 7},
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _prepare(self, *, diff: str = SOURCE_DIFF, policy: TargetPolicy | None = None):
        return prepare_submission(
            _run(),
            self.run_directory,
            diff=diff,
            policy=policy or _policy(),
            destination=self.run_directory / "submission",
            branch="mailman/issue-7",
            title="thing does not strip",
        )

    def test_a_clean_run_against_a_permissive_policy_is_ready(self) -> None:
        record = self._prepare()
        self.assertTrue(record["ready"], record["blocking_codes"])
        self.assertEqual(record["issue_number"], 7)
        body = (self.run_directory / "submission" / "pull-request.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Closes #7.", body)
        self.assertIn("python -m pytest -q", body)

    def test_a_forbidding_policy_blocks(self) -> None:
        record = self._prepare(policy=_policy(stance="forbidden"))
        self.assertFalse(record["ready"])
        self.assertIn("policy-forbids-ai", record["blocking_codes"])

    def test_an_unread_policy_blocks(self) -> None:
        record = self._prepare(policy=_policy(stance="unknown"))
        self.assertIn("policy-unread", record["blocking_codes"])

    def test_a_missing_changelog_entry_blocks(self) -> None:
        record = self._prepare(policy=_policy(changelog_directory="changelog"))
        self.assertIn("missing-changelog-entry", record["blocking_codes"])

    def test_a_mandatory_duplicate_search_blocks_until_recorded(self) -> None:
        policy = _policy(requires_duplicate_search=True)
        self.assertIn("missing-duplicate-search", self._prepare(policy=policy)["blocking_codes"])
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-02T00:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [],
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(self._prepare(policy=policy)["ready"])

    def test_a_search_that_partly_failed_does_not_clear_the_gate(self) -> None:
        # Issue #30: both `gh search` calls exited 1 on encode/starlette and the
        # `--search` fallbacks returned [], and the record still said success.
        policy = _policy(requires_duplicate_search=True)
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-02T00:00:00+00:00",
                    "success": True,
                    "complete": False,
                    "failed_methods": [
                        {"kind": "pr", "method": "listing", "detail": "boom"}
                    ],
                    "matches": [],
                }
            ),
            encoding="utf-8",
        )
        record = self._prepare(policy=policy)
        self.assertFalse(record["ready"])
        self.assertIn("degraded-duplicate-search", record["blocking_codes"])

    def test_a_recorded_match_blocks_whatever_the_policy_asks(self) -> None:
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-02T00:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [
                        {
                            "number": 3485,
                            "title": "Delay background task execution",
                            "state": "OPEN",
                            "pull_request": True,
                            "matched_by": ["#3458"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        record = self._prepare()
        self.assertFalse(record["ready"])
        self.assertIn("possible-duplicate", record["blocking_codes"])

    def test_a_failed_duplicate_search_does_not_count_as_one(self) -> None:
        policy = _policy(requires_duplicate_search=True)
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-02T00:00:00+00:00",
                    "success": False,
                    "detail": "the prs search failed",
                    "matches": [],
                }
            ),
            encoding="utf-8",
        )
        record = self._prepare(policy=policy)
        self.assertIn("missing-duplicate-search", record["blocking_codes"])
        self.assertFalse(record["duplicate_search_recorded"])

    def test_the_runs_own_issue_is_never_its_own_duplicate(self) -> None:
        # Issue #31: the run's issue matched itself and blocked every run.
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-03T00:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [
                        {
                            "number": 7,
                            "title": "thing does not strip",
                            "state": "open",
                            "pull_request": False,
                            "matched_by": ["search"],
                            "methods": ["search"],
                            "matched_terms": [],
                            "term_count": 4,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        record = self._prepare()
        self.assertTrue(record["ready"], record["blocking_codes"])

    def test_a_weak_listing_match_blocks_as_unreviewed_not_as_a_duplicate(self) -> None:
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-03T00:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [_weak_match(1386)],
                }
            ),
            encoding="utf-8",
        )
        record = self._prepare()
        self.assertFalse(record["ready"])
        self.assertIn("unreviewed-duplicate-candidates", record["blocking_codes"])
        self.assertNotIn("possible-duplicate", record["blocking_codes"])

    def test_an_acknowledgement_clears_the_weak_matches_it_names(self) -> None:
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-03T00:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [_weak_match(1386)],
                }
            ),
            encoding="utf-8",
        )
        record_duplicate_acknowledgement(
            self.run_directory, note="read all four, none touch the broker"
        )
        record = self._prepare()
        self.assertTrue(record["ready"], record["blocking_codes"])

    def test_an_acknowledgement_does_not_cover_a_match_it_never_saw(self) -> None:
        path = self.run_directory / "duplicate-search.json"
        path.write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-03T00:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [_weak_match(1386)],
                }
            ),
            encoding="utf-8",
        )
        record_duplicate_acknowledgement(self.run_directory, note="read it")
        path.write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-03T01:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [_weak_match(1386), _weak_match(1400)],
                }
            ),
            encoding="utf-8",
        )
        record = self._prepare()
        self.assertFalse(record["ready"])
        self.assertIn("unreviewed-duplicate-candidates", record["blocking_codes"])

    def test_a_merged_match_blocks_as_already_fixed_upstream(self) -> None:
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-03T00:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [dict(_weak_match(1), state="merged", methods=["search"])],
                }
            ),
            encoding="utf-8",
        )
        record = self._prepare()
        self.assertFalse(record["ready"])
        self.assertIn("already-fixed-upstream", record["blocking_codes"])
        self.assertNotIn("possible-duplicate", record["blocking_codes"])

    def _merged_match_already_in_base(self, *, reproduced: bool) -> None:
        """Record a merged match whose merge commit precedes the base commit."""
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-03T00:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [dict(_weak_match(1), state="merged", methods=["search"])],
                }
            ),
            encoding="utf-8",
        )
        workspace = self.run_directory / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)

        def git(*arguments: str) -> str:
            completed = subprocess.run(
                ["git", "-C", str(workspace), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            return completed.stdout.strip()

        git("init", "--quiet")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        (workspace / "core.py").write_text("first\n", encoding="utf-8")
        git("add", "core.py")
        git("commit", "--quiet", "-m", "the merged fix")
        merged = git("rev-parse", "HEAD")
        (workspace / "core.py").write_text("second\n", encoding="utf-8")
        git("add", "core.py")
        git("commit", "--quiet", "-m", "later work")
        base = git("rev-parse", "HEAD")
        (self.run_directory / "workspace.json").write_text(
            json.dumps({"head": base, "clean": True}), encoding="utf-8"
        )
        (self.run_directory / "reproduction.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "machine_checked": True,
                    "reproduced": reproduced,
                    "base_commit": base,
                }
            ),
            encoding="utf-8",
        )
        (self.run_directory / "prior-art.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "attempts": [
                        {
                            "number": 1,
                            "outcome": "merged",
                            "url": "https://github.com/example/project/pull/1",
                            "merge_commit": merged,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_a_merged_match_already_in_the_base_commit_does_not_block(self) -> None:
        """See https://github.com/wolfgang-aura/Mailman/issues/46."""
        self._merged_match_already_in_base(reproduced=True)
        record = self._prepare()
        self.assertNotIn("already-fixed-upstream", record["blocking_codes"])

    def test_a_merged_match_in_base_still_blocks_without_a_reproduction(self) -> None:
        self._merged_match_already_in_base(reproduced=False)
        record = self._prepare()
        self.assertIn("already-fixed-upstream", record["blocking_codes"])

    def test_a_closed_attempt_can_be_acknowledged(self) -> None:
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-03T00:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [
                        dict(_weak_match(39682), state="closed", methods=["search"])
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.assertIn(
            "unreviewed-duplicate-candidates", self._prepare()["blocking_codes"]
        )
        record_duplicate_acknowledgement(
            self.run_directory, note="a bot closed it unread, not a rejection"
        )
        self.assertTrue(self._prepare()["ready"], self._prepare()["blocking_codes"])

    def test_an_acknowledgement_cannot_clear_a_strong_match(self) -> None:
        (self.run_directory / "duplicate-search.json").write_text(
            json.dumps(
                {
                    "searched_at": "2026-09-03T00:00:00+00:00",
                    "success": True,
                    "complete": True,
                    "matches": [
                        {
                            "number": 3485,
                            "title": "Delay background task execution",
                            "state": "OPEN",
                            "pull_request": True,
                            "matched_by": ["#7"],
                            "methods": ["listing"],
                            "matched_terms": [],
                            "term_count": 4,
                            "references_issue": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        record_duplicate_acknowledgement(self.run_directory, note="looked")
        record = self._prepare()
        self.assertFalse(record["ready"])
        self.assertIn("possible-duplicate", record["blocking_codes"])

    def test_a_maintainer_assignment_requirement_blocks(self) -> None:
        record = self._prepare(policy=_policy(requires_maintainer_assignment=True))
        self.assertIn("needs-maintainer-assignment", record["blocking_codes"])

    def test_a_forbidden_trailer_is_spelled_out_in_the_draft(self) -> None:
        self._prepare(policy=_policy(ai_trailer="forbidden"))
        body = (self.run_directory / "submission" / "pull-request.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Do not add any AI co-author trailer", body)

    def test_a_required_trailer_names_the_agent(self) -> None:
        self._prepare(
            policy=_policy(ai_trailer="required", ai_trailer_form="Assisted-by: {agent}")
        )
        body = (self.run_directory / "submission" / "pull-request.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Assisted-by: codex", body)

    def test_a_disclosure_requirement_puts_the_line_in_the_body(self) -> None:
        self._prepare(policy=_policy(disclosure_required=True))
        body = (self.run_directory / "submission" / "pull-request.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("drafted with AI assistance", body)
        self.assertNotIn("> This change was drafted", body)

    def test_an_unfinished_run_blocks(self) -> None:
        run = _run()
        run.status = RunStatus.BLOCKED
        record = prepare_submission(
            run,
            self.run_directory,
            diff=SOURCE_DIFF,
            policy=_policy(),
            destination=self.run_directory / "submission",
            branch="mailman/issue-7",
            title="thing does not strip",
        )
        self.assertIn("run-not-ready", record["blocking_codes"])

    def test_a_run_with_no_passing_verification_blocks(self) -> None:
        (self.run_directory / "verification.json").write_text(
            json.dumps([{"command": ["python", "-m", "pytest", "-q"], "exit_code": 1}]),
            encoding="utf-8",
        )
        record = self._prepare()
        self.assertIn("no-passing-verification", record["blocking_codes"])

    def test_the_draft_refuses_to_suggest_the_issue_title(self) -> None:
        self._prepare()
        body = (self.run_directory / "submission" / "pull-request.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("The working title above is a placeholder", body)
        self.assertIn("gh pr list --repo example/project", body)

    def test_the_draft_orders_the_body_and_asks_for_the_alternative(self) -> None:
        self._prepare()
        body = (self.run_directory / "submission" / "pull-request.md").read_text(
            encoding="utf-8"
        )
        self.assertLess(
            body.index("the observable symptom"), body.index("How this was tested")
        )
        self.assertIn("An alternative I did not take", body)
        self.assertIn("Opened as a real pull request, not a draft", body)

    def test_the_draft_names_the_hosts_the_results_came_from(self) -> None:
        (self.run_directory / "verification.json").write_text(
            json.dumps(
                [
                    {
                        "command": ["python", "-m", "pytest", "-q"],
                        "exit_code": 0,
                        "environment": {
                            "operating_system": "Windows",
                            "python_version": "3.14.3",
                        },
                    }
                ]
            ),
            encoding="utf-8",
        )
        self._prepare()
        body = (self.run_directory / "submission" / "pull-request.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("- Windows / Python 3.14.3", body)

    def test_an_unrecorded_environment_is_not_reported_as_unknown(self) -> None:
        self._prepare()
        body = (self.run_directory / "submission" / "pull-request.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("unknown / Python", body)

    def test_the_disclosure_names_the_model_when_one_was_recorded(self) -> None:
        run = _run()
        run = RunRecord(
            run_id=run.run_id,
            repository=run.repository,
            issue=run.issue,
            base_commit=run.base_commit,
            primary=AgentConfig(agent="codex", model="gpt-5.6-luna"),
            reviewer=AgentConfig(agent="claude", model="claude-sonnet-5"),
            status=run.status,
        )
        prepare_submission(
            run,
            self.run_directory,
            diff=SOURCE_DIFF,
            policy=_policy(disclosure_required=True),
            destination=self.run_directory / "submission",
            branch="mailman/issue-7",
            title="thing does not strip",
        )
        body = (self.run_directory / "submission" / "pull-request.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("codex (`gpt-5.6-luna`)", body)
        self.assertIn("claude (`claude-sonnet-5`)", body)

    def test_the_accountability_brief_is_written(self) -> None:
        self._prepare()
        brief = (self.run_directory / "submission" / "accountability.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Before you open this pull request", brief)


if __name__ == "__main__":
    unittest.main()


class NoTestAcknowledgementRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.run_directory = Path(self._temporary.name)

    def test_the_record_pins_the_paths_the_diff_touched(self) -> None:
        source_only = SOURCE_DIFF.split("diff --git a/tests")[0]
        record = record_no_test_acknowledgement(
            self.run_directory, note="the suite already fails harder", diff=source_only
        )
        self.assertEqual(record["covered_paths"], ["src/thing.py"])
        stored = json.loads(
            (self.run_directory / "no-test-acknowledgement.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(stored["note"], "the suite already fails harder")

    def test_an_empty_note_is_refused(self) -> None:
        source_only = SOURCE_DIFF.split("diff --git a/tests")[0]
        with self.assertRaises(ValueError):
            record_no_test_acknowledgement(
                self.run_directory, note="   ", diff=source_only
            )

    def test_a_diff_that_already_changes_a_test_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            record_no_test_acknowledgement(
                self.run_directory, note="why", diff=SOURCE_DIFF
            )

    def test_an_empty_diff_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            record_no_test_acknowledgement(self.run_directory, note="why", diff="")
