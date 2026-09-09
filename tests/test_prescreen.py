"""Most targets fail the pre-screen, and failing before a run exists is the point.

https://github.com/wolfgang-aura/Mailman/issues/75
"""
import json
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

from mailman.cli import main
from mailman.prescreen import (
    DECIDABLE,
    PRESCREEN_HOURS,
    check,
    is_fresh,
    issue_reference,
    load_prescreen,
    prescreen_directory,
    prescreen_issue,
    prescreen_path,
)
from mailman.targeting import (
    NO_REPRODUCTION,
    NO_TARGET_INTEL,
    OPEN_PULL_REQUEST,
    assess_target,
)


class IssueReferenceTests(unittest.TestCase):
    def test_reads_short_form_and_url(self) -> None:
        self.assertEqual(issue_reference("pdm-project/pdm#3877"), ("pdm-project/pdm", 3877))
        self.assertEqual(
            issue_reference("https://github.com/pdm-project/pdm/issues/3877"),
            ("pdm-project/pdm", 3877),
        )

    def test_refuses_a_repository_without_an_issue(self) -> None:
        with self.assertRaises(ValueError):
            issue_reference("pdm-project/pdm")


class PrescreenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "runs"
        self.root.mkdir(parents=True)

    def stub(self, payload: str, issue_payload: dict | None = None) -> str:
        directory = Path(self.temporary.name) / "bin"
        directory.mkdir(exist_ok=True)
        (directory / "payload.json").write_text(payload, encoding="utf-8")
        issue = issue_payload or {
            "number": 7,
            "title": "Crash on empty input",
            "body": "The command crashes on empty input.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }
        (directory / "issue-payload.json").write_text(
            json.dumps(issue), encoding="utf-8"
        )
        if sys.platform == "win32":
            stub = directory / "gh.cmd"
            stub.write_text(
                '@echo off\r\nif "%1"=="issue" if "%2"=="view" ('
                'type "%~dp0issue-payload.json" & exit /b 0)\r\n'
                'type "%~dp0payload.json"\r\n',
                encoding="utf-8",
            )
            return str(stub)
        stub = directory / "gh.sh"
        stub.write_text(
            '#!/bin/sh\nif [ "$1" = issue ] && [ "$2" = view ]; then '
            'cat "$(dirname "$0")/issue-payload.json"; else '
            'cat "$(dirname "$0")/payload.json"; fi\n',
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return str(stub)

    def test_a_clean_issue_passes_and_records_where_it_looked(self) -> None:
        record = prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["blocking"], [])
        self.assertTrue(record["duplicate_search"]["success"])
        self.assertIn("init-run", record["next"])
        self.assertEqual(load_prescreen(self.root, "example/project", 7), record)
        self.assertEqual(record["issue"]["title"], "Crash on empty input")

    def test_a_feature_request_is_rejected_before_a_run_exists(self) -> None:
        issue = {
            "number": 7,
            "title": "Choose and add a new transport",
            "body": "Several routing designs are possible.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [{"name": "feature"}],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", issue),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn("issue-not-bounded-fix", record["blocking"])
        self.assertNotIn("duplicate_search", record)
        self.assertEqual(
            record["stages_skipped"], ["duplicate-search", "prior-art", "claims"]
        )

    def test_the_verdict_lands_beside_the_repository_screens(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        path = prescreen_path(self.root, "example/project", 7)
        self.assertTrue(path.is_file())
        self.assertEqual(path.name, "example__project__7.json")

    def test_no_run_directory_is_created(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        runs = [entry for entry in self.root.iterdir() if entry.name != "issue-screens"]
        self.assertEqual(runs, [])

    def test_an_open_rival_rejects_the_issue_before_a_run_exists(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        directory = prescreen_directory(self.root, "example/project", 7)
        search = json.loads((directory / "duplicate-search.json").read_text(encoding="utf-8"))
        search["matches"] = [{
            "number": 99, "title": "Fix the thing", "state": "open",
            "pull_request": True, "references_issue": True, "matched_by": ["#7"],
        }]
        (directory / "duplicate-search.json").write_text(json.dumps(search), encoding="utf-8")
        assessment = assess_target(directory)
        self.assertIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertIn(OPEN_PULL_REQUEST, DECIDABLE)

    def test_a_verdict_never_rests_on_something_this_stage_cannot_know(self) -> None:
        record = prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        # There is no clone here, so `assess_target` always wants a
        # reproduction and target intel. Neither is this stage's question.
        directory = prescreen_directory(self.root, "example/project", 7)
        self.assertIn(NO_REPRODUCTION, assess_target(directory).blocking)
        self.assertNotIn(NO_REPRODUCTION, record["blocking"])
        self.assertNotIn(NO_TARGET_INTEL, record["blocking"])

    def test_check_refuses_an_issue_with_no_pre_screen(self) -> None:
        record, refusal = check(self.root, "example/project#7")
        self.assertIsNone(record)
        self.assertIn("no pre-screen", refusal)

    def test_check_refuses_a_rejected_issue(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        path = prescreen_path(self.root, "example/project", 7)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored.update(verdict="reject", blocking=["open-pull-request"])
        path.write_text(json.dumps(stored), encoding="utf-8")
        _, refusal = check(self.root, "example/project#7")
        self.assertIn("open-pull-request", refusal)

    def test_check_refuses_a_stale_pre_screen(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        path = prescreen_path(self.root, "example/project", 7)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["screened_at"] = (
            datetime.now(UTC) - timedelta(hours=PRESCREEN_HOURS + 1)
        ).isoformat()
        path.write_text(json.dumps(stored), encoding="utf-8")
        self.assertFalse(is_fresh(stored))
        _, refusal = check(self.root, "example/project#7")
        self.assertIn("older than", refusal)

    def test_check_refuses_a_pre_classification_schema(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        path = prescreen_path(self.root, "example/project", 7)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["schema_version"] = 1
        path.write_text(json.dumps(stored), encoding="utf-8")

        _, refusal = check(self.root, "example/project#7")

        self.assertIn("predates issue classification", refusal)

    def test_check_clears_a_fresh_pass(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        record, refusal = check(self.root, "example/project#7")
        self.assertIsNone(refusal)
        self.assertEqual(record["verdict"], "pass")


class InitRunGateTests(PrescreenTests):
    def init_run(self, *extra: str) -> tuple[int, str]:
        out, err = StringIO(), StringIO()
        arguments = [
            "init-run", "--repository", "https://github.com/example/project.git",
            "--issue", "https://github.com/example/project/issues/7",
            "--base-commit", "a" * 40, "--primary", "codex", "--reviewer", "claude",
            "--primary-model", "m", "--reviewer-model", "m",
            "--data-root", str(self.root), *extra,
        ]
        with redirect_stdout(out), redirect_stderr(err):
            code = main(arguments)
        return code, out.getvalue() + err.getvalue()

    def test_init_run_refuses_an_issue_that_was_never_pre_screened(self) -> None:
        code, output = self.init_run()
        self.assertEqual(code, 2)
        self.assertIn("no pre-screen", output)

    def test_init_run_proceeds_after_a_passing_pre_screen(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        code, output = self.init_run()
        self.assertEqual(code, 0)
        self.assertIn("run_id", output)
        run_id = json.loads(output)["run_id"]
        self.assertTrue((self.root / run_id / "prescreen.json").is_file())

    def test_skipping_the_pre_screen_records_the_reason(self) -> None:
        code, output = self.init_run("--no-prescreen", "operator asked for this one")
        self.assertEqual(code, 0)
        run_id = json.loads(output)["run_id"]
        skipped = json.loads(
            (self.root / run_id / "prescreen-skipped.json").read_text(encoding="utf-8")
        )
        self.assertEqual(skipped["reason"], "operator asked for this one")


if __name__ == "__main__":
    unittest.main()
