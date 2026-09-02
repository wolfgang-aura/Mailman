from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mailman.models import AgentConfig, RunRecord, RunStatus
from mailman.submission import (
    TargetPolicy,
    _match_rows,
    analyze_diff,
    prepare_submission,
)


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
        self.assertEqual(names, ["attrs.json", "pytest.json", "starlette.json"])
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
                    "matches": [],
                }
            ),
            encoding="utf-8",
        )
        self.assertTrue(self._prepare(policy=policy)["ready"])

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

    def test_the_accountability_brief_is_written(self) -> None:
        self._prepare()
        brief = (self.run_directory / "submission" / "accountability.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Before you open this pull request", brief)


if __name__ == "__main__":
    unittest.main()
