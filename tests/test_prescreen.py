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
from mailman.hunt import create_hunt, hunt_path, save
from mailman.prescreen import (
    DECIDABLE,
    PRESCREEN_HOURS,
    TRIVIAL,
    TRIVIAL_FIX,
    TRIVIAL_FIX_DIRECT_PUSH,
    UNKNOWN,
    check,
    estimate_fix_size,
    is_fresh,
    issue_reference,
    issue_symbols,
    load_prescreen,
    prescreen_directory,
    prescreen_issue,
    prescreen_path,
)
from mailman.screen import screen_path
from mailman.targeting import (
    NO_REPRODUCTION,
    NO_TARGET_INTEL,
    OPEN_PULL_REQUEST,
    assess_target,
)


class IssueReferenceTests(unittest.TestCase):
    def test_reads_short_form_and_url(self) -> None:
        self.assertEqual(
            issue_reference("pdm-project/pdm#3877"), ("pdm-project/pdm", 3877)
        )
        self.assertEqual(
            issue_reference("https://github.com/pdm-project/pdm/issues/3877"),
            ("pdm-project/pdm", 3877),
        )

    def test_refuses_a_repository_without_an_issue(self) -> None:
        with self.assertRaises(ValueError):
            issue_reference("pdm-project/pdm")


class FixSizeTests(unittest.TestCase):
    """What the issue says it wants, read for how long the change would take.

    https://github.com/wolfgang-aura/Mailman/issues/79
    """

    def test_a_documentation_typo_reads_as_trivial(self) -> None:
        estimate, reason = estimate_fix_size("Typo in the README", "", [])
        self.assertEqual(estimate, TRIVIAL)
        self.assertIn("typo", reason)

    def test_documentation_that_contradicts_the_code_reads_as_trivial(self) -> None:
        # pdm-project/pdm#3877, the issue behind the wasted run: one line in
        # docs/reference/pep621.md.
        estimate, reason = estimate_fix_size(
            "pep621 reference is outdated",
            "The documentation says the field is `project.name`, which is wrong.",
            [],
        )
        self.assertEqual(estimate, TRIVIAL)
        self.assertIn("documentation wording", reason)

    def test_a_wrong_error_message_reads_as_trivial(self) -> None:
        estimate, _ = estimate_fix_size(
            "Error message for an empty path is misleading", "", []
        )
        self.assertEqual(estimate, TRIVIAL)

    def test_a_version_pin_bump_reads_as_trivial(self) -> None:
        estimate, _ = estimate_fix_size(
            "Relax the upper bound on the packaging requirement", "", []
        )
        self.assertEqual(estimate, TRIVIAL)

    def test_a_typo_label_is_enough_on_its_own(self) -> None:
        estimate, reason = estimate_fix_size("Something is off", "", ["Typo"])
        self.assertEqual(estimate, TRIVIAL)
        self.assertEqual(reason, "labelled typo")

    def test_an_ordinary_defect_is_left_unknown(self) -> None:
        # The estimate never claims a change is large. It only names the ones
        # it can see are small.
        estimate, reason = estimate_fix_size(
            "Crash on empty input",
            "The parser raises IndexError when the file has no rows.",
            ["bug"],
        )
        self.assertEqual(estimate, UNKNOWN)
        self.assertIn("nothing in the issue", reason)


class IssueSymbolTests(unittest.TestCase):
    def test_reads_backticked_names_dotted_paths_and_files(self) -> None:
        body = (
            "The pipeline calls {b}_handle_upserts{b} and {b}_ahandle_upserts{b} in "
            "llama_index/core/ingestion/pipeline.py; {b}docstore{b} is a word. "
            "See {b}IngestionPipeline.run(){b}, {b}Pipeline._handle_upserts{b} "
            "and {b}x{b}, then {b}a_b{b}."
        ).format(b="`")
        # A dotted path reduces to its last segment, so the same function named
        # two ways is one symbol, and a plain word like `run` is not one.
        self.assertEqual(
            issue_symbols(body),
            ["_handle_upserts", "_ahandle_upserts", "a_b", "pipeline.py"],
        )

    def test_the_count_is_capped_because_each_symbol_is_a_search_call(self) -> None:
        body = " ".join(f"`name_{index}`" for index in range(20))
        self.assertEqual(len(issue_symbols(body)), 4)

    def test_prose_between_two_short_tokens_is_not_a_name(self) -> None:
        self.assertEqual(issue_symbols("`x` and `y` then `real_one`"), ["real_one"])


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
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]")
        )
        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["blocking"], [])
        self.assertTrue(record["duplicate_search"]["success"])
        self.assertIn("init-run", record["next"])
        self.assertEqual(load_prescreen(self.root, "example/project", 7), record)
        self.assertEqual(record["issue"]["title"], "Crash on empty input")
        directory = prescreen_directory(self.root, "example/project", 7)
        search = json.loads(
            (directory / "duplicate-search.json").read_text(encoding="utf-8")
        )
        self.assertEqual(search["query"], "Crash on empty input")

    def test_cli_binds_pre_run_screening_to_the_single_live_hunt(self) -> None:
        hunt = create_hunt(
            self.root,
            1,
            primary="codex",
            primary_model="fixture-primary",
            reviewer="claude",
            reviewer_model="fixture-reviewer",
        )
        hunt["deadline_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        save(hunt_path(self.root, hunt["hunt_id"]), hunt)
        stderr = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(stderr):
            code = main(
                [
                    "prescreen",
                    "example/project#7",
                    "--executable",
                    self.stub("[]"),
                    "--data-root",
                    str(self.root),
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("deadline expired", stderr.getvalue())

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

    def record_direct_push_share(self, share: float) -> None:
        """Write the screen record the pre-screen reads the habit out of."""
        path = screen_path(self.root, "example/project")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "repository": "example/project",
                    "success": True,
                    "verdict": "pass",
                    "gates": [
                        {
                            "name": "direct-push",
                            "passed": True,
                            "blocking": False,
                            "detail": "",
                            "data": {"direct_push_share": share},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def typo_issue(self) -> dict:
        return {
            "number": 7,
            "title": "Typo in the installation docs",
            "body": "`pip instal` should read `pip install`.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }

    def test_a_trivial_fix_where_the_maintainer_pushes_directly_is_rejected(
        self,
    ) -> None:
        self.record_direct_push_share(0.8)
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", self.typo_issue()),
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn(TRIVIAL_FIX_DIRECT_PUSH, record["blocking"])
        self.assertEqual(record["fix_size"]["estimate"], TRIVIAL)
        self.assertEqual(record["fix_size"]["direct_push_share"], 0.8)
        self.assertIn("before he reviews it", record["fix_size"]["detail"])
        self.assertIn("before he reviews it", record["next"])
        self.assertNotIn("duplicate_search", record)
        self.assertEqual(
            record["stages_skipped"], ["duplicate-search", "prior-art", "claims"]
        )

    def test_the_cli_exits_non_zero_on_a_trivial_fix_reject(self) -> None:
        self.record_direct_push_share(0.8)
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(
                [
                    "prescreen",
                    "example/project#7",
                    "--executable",
                    self.stub("[]", self.typo_issue()),
                    "--data-root",
                    str(self.root),
                ]
            )

        self.assertEqual(code, 1)

    def test_a_trivial_fix_in_a_reviewed_repository_is_only_a_warning(self) -> None:
        self.record_direct_push_share(0.05)
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", self.typo_issue()),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["blocking"], [])
        self.assertIn(TRIVIAL_FIX, record["warnings"])
        self.assertEqual(record["fix_size"]["estimate"], TRIVIAL)
        self.assertIn("reads as trivial", record["fix_size"]["detail"])

    def test_an_unscreened_repository_leaves_the_habit_unknown(self) -> None:
        record = prescreen_issue(
            self.root,
            "example/project#7",
            executable=self.stub("[]", self.typo_issue()),
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertIn(TRIVIAL_FIX, record["warnings"])
        self.assertIsNone(record["fix_size"]["direct_push_share"])
        self.assertIn("unrecorded", record["fix_size"]["detail"])

    def test_an_ordinary_defect_records_an_unknown_fix_size(self) -> None:
        self.record_direct_push_share(0.9)
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]")
        )

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(record["warnings"], [])
        self.assertEqual(record["fix_size"]["estimate"], UNKNOWN)
        self.assertEqual(record["fix_size"]["direct_push_share"], 0.9)

    def test_each_symbol_in_the_issue_body_gets_its_own_narrow_search(self) -> None:
        # llama_index#22639: the body named the functions, two open rivals
        # carried them, neither mentioned the issue number, and the typed
        # symbols were prose words. One joined query of every term found
        # nothing, because GitHub ANDs the terms; one query per symbol found
        # both rivals. Mailman #96.
        issue = {
            "number": 7,
            "title": "Docstore delete fails on failed runs",
            "body": "`_handle_upserts` and `_ahandle_upserts` skip the delete.",
            "state": "OPEN",
            "url": "https://github.com/example/project/issues/7",
            "author": {"login": "reporter"},
            "labels": [],
            "createdAt": "2026-09-01T00:00:00Z",
            "updatedAt": "2026-09-01T00:00:00Z",
        }
        record = prescreen_issue(
            self.root,
            "example/project#7",
            symbols=["docstore"],
            executable=self.stub("[]", issue),
        )
        self.assertEqual(record["symbols"], ["docstore"])
        self.assertEqual(
            record["issue_symbols"], ["_handle_upserts", "_ahandle_upserts"]
        )
        directory = prescreen_directory(self.root, "example/project", 7)
        search = json.loads(
            (directory / "duplicate-search.json").read_text(encoding="utf-8")
        )
        self.assertEqual(search["symbols"], ["docstore"])
        self.assertEqual(
            search["issue_symbols"], ["_handle_upserts", "_ahandle_upserts"]
        )
        queries = {}
        for command in search["commands"]:
            if command["method"] != "narrow":
                continue
            argv = command["command"]
            queries.setdefault(argv[1], []).append(argv[argv.index("--search") + 1])
        self.assertEqual(
            queries["pr"],
            ["#7 docstore", "#7 _handle_upserts", "#7 _ahandle_upserts"],
        )
        self.assertEqual(queries["issue"], ["#7 docstore"])

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
        search = json.loads(
            (directory / "duplicate-search.json").read_text(encoding="utf-8")
        )
        search["matches"] = [
            {
                "number": 99,
                "title": "Fix the thing",
                "state": "open",
                "pull_request": True,
                "references_issue": True,
                "matched_by": ["#7"],
            }
        ]
        (directory / "duplicate-search.json").write_text(
            json.dumps(search), encoding="utf-8"
        )
        assessment = assess_target(directory)
        self.assertIn(OPEN_PULL_REQUEST, assessment.blocking)
        self.assertIn(OPEN_PULL_REQUEST, DECIDABLE)

    def test_title_search_rejects_an_open_semantic_rival_before_setup(self) -> None:
        rival = [
            {
                "number": 99,
                "title": "Fix crash on empty input",
                "state": "open",
                "url": "https://github.com/example/project/pull/99",
                "createdAt": "2026-09-02T00:00:00Z",
                "body": "Handle the empty-input crash.",
                "headRefName": "fix-empty-input",
            }
        ]

        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub(json.dumps(rival))
        )

        self.assertEqual(record["verdict"], "reject")
        self.assertIn(OPEN_PULL_REQUEST, record["blocking"])
        self.assertEqual(record["open_attempts"], [99])

    def test_a_verdict_never_rests_on_something_this_stage_cannot_know(self) -> None:
        record = prescreen_issue(
            self.root, "example/project#7", executable=self.stub("[]")
        )
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

    def test_check_refuses_a_superseded_schema(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        path = prescreen_path(self.root, "example/project", 7)
        stored = json.loads(path.read_text(encoding="utf-8"))
        stored["schema_version"] = 1
        path.write_text(json.dumps(stored), encoding="utf-8")

        _, refusal = check(self.root, "example/project#7")

        self.assertIn("predates a screening question", refusal)

    def test_check_clears_a_fresh_pass(self) -> None:
        prescreen_issue(self.root, "example/project#7", executable=self.stub("[]"))
        record, refusal = check(self.root, "example/project#7")
        self.assertIsNone(refusal)
        self.assertEqual(record["verdict"], "pass")


class InitRunGateTests(PrescreenTests):
    def init_run(self, *extra: str) -> tuple[int, str]:
        out, err = StringIO(), StringIO()
        arguments = [
            "init-run",
            "--repository",
            "https://github.com/example/project.git",
            "--issue",
            "https://github.com/example/project/issues/7",
            "--base-commit",
            "a" * 40,
            "--primary",
            "codex",
            "--reviewer",
            "claude",
            "--primary-model",
            "m",
            "--reviewer-model",
            "m",
            "--data-root",
            str(self.root),
            *extra,
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
