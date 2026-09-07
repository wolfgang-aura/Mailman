from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from mailman.review_decision import (
    DECISION_FILENAME,
    DecisionError,
    blank_decision,
    load_decision,
    parse_decision,
    render_gaps,
    render_panels,
    render_questions,
)


VALID = {
    "schema_version": 1,
    "recommendation": "SEND",
    "headline": "The crash is fixed and the target suite is green on both interpreters.",
    "panels": {
        "broken": {
            "claim": "Every empty payload raised instead of returning the default.",
            "detail": "Reproduced at the base commit before any edit was made.",
            "evidence": "machine-checked",
        },
        "did": {
            "claim": "The empty case now returns the documented default.",
            "detail": "One function changed, one regression test added.",
            "evidence": "machine-checked",
        },
        "fixed": {
            "claim": "The reproduction passes and nothing else moved.",
            "detail": "Identical failures on the base and patched trees.",
            "evidence": "measured",
        },
    },
    "questions": [
        {
            "question": "Send this upstream now, or wait for the maintainer?",
            "blocking": True,
            "options": [
                {"label": "A", "text": "Open the pull request today.", "cost": "None."},
                {"label": "B", "text": "Wait for the thread.", "cost": "Days of delay."},
            ],
            "recommendation": "A - the issue is unassigned and stale.",
        }
    ],
    "gaps": [
        {
            "gap": "macOS behaviour was never observed.",
            "why_open": "No macOS host is available here.",
            "cost_to_close": "One CI job on a macOS runner.",
        }
    ],
    "ledger": [
        {
            "claim": "The target suite passes.",
            "kind": "machine-checked",
            "evidence": "commands/0001-pytest.json, exit 0",
        }
    ],
}


def without(**changes: object) -> dict:
    document = copy.deepcopy(VALID)
    document.update(changes)
    return document


class DecisionValidationTests(unittest.TestCase):
    def test_a_complete_decision_parses(self) -> None:
        decision = parse_decision(copy.deepcopy(VALID))

        self.assertEqual(decision.recommendation, "SEND")
        self.assertEqual([panel.key for panel in decision.panels], ["broken", "did", "fixed"])
        self.assertEqual(len(decision.blocking_questions), 1)

    def test_a_statement_is_not_a_question(self) -> None:
        document = copy.deepcopy(VALID)
        document["questions"][0]["question"] = "We should probably send this upstream."

        with self.assertRaises(DecisionError) as caught:
            parse_decision(document)

        self.assertTrue(
            any("question mark" in problem for problem in caught.exception.problems),
            caught.exception.problems,
        )

    def test_an_option_without_a_cost_is_refused(self) -> None:
        document = copy.deepcopy(VALID)
        document["questions"][0]["options"][1]["cost"] = ""

        with self.assertRaises(DecisionError) as caught:
            parse_decision(document)

        self.assertTrue(
            any(".cost is empty" in problem for problem in caught.exception.problems),
            caught.exception.problems,
        )

    def test_options_are_labelled_in_order(self) -> None:
        document = copy.deepcopy(VALID)
        document["questions"][0]["options"][1]["label"] = "C"

        with self.assertRaises(DecisionError) as caught:
            parse_decision(document)

        self.assertTrue(
            any("labels run A, B, C" in problem for problem in caught.exception.problems),
            caught.exception.problems,
        )

    def test_a_gap_needs_a_reason_and_a_price(self) -> None:
        document = copy.deepcopy(VALID)
        document["gaps"][0]["why_open"] = ""
        document["gaps"][0]["cost_to_close"] = ""

        with self.assertRaises(DecisionError) as caught:
            parse_decision(document)

        problems = " ".join(caught.exception.problems)
        self.assertIn("why_open is empty", problems)
        self.assertIn("cost_to_close is empty", problems)

    def test_an_invented_evidence_class_is_refused(self) -> None:
        document = copy.deepcopy(VALID)
        document["ledger"][0]["kind"] = "verified"

        with self.assertRaises(DecisionError) as caught:
            parse_decision(document)

        self.assertTrue(
            any("ledger[1].kind" in problem for problem in caught.exception.problems),
            caught.exception.problems,
        )

    def test_every_problem_is_reported_at_once(self) -> None:
        """A model fixing one complaint per round trip never finishes."""
        with self.assertRaises(DecisionError) as caught:
            parse_decision({"schema_version": 1})

        self.assertGreater(len(caught.exception.problems), 4)

    def test_no_questions_is_allowed_when_stated(self) -> None:
        decision = parse_decision(without(questions=[]))

        self.assertEqual(decision.questions, [])
        self.assertIn("Nothing is blocked on you", render_questions(decision))

    def test_the_skeleton_does_not_pass_as_written(self) -> None:
        with self.assertRaises(DecisionError):
            parse_decision(blank_decision())


class DecisionFileTests(unittest.TestCase):
    def test_a_missing_file_names_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(DecisionError) as caught:
                load_decision(Path(temporary_directory))

        self.assertIn(DECISION_FILENAME, " ".join(caught.exception.problems))

    def test_a_valid_file_loads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / DECISION_FILENAME).write_text(
                json.dumps(VALID), encoding="utf-8"
            )
            decision = load_decision(directory)

        self.assertEqual(decision.recommendation, "SEND")


class DecisionRenderingTests(unittest.TestCase):
    def test_the_panels_carry_a_claim_a_detail_and_a_stamp(self) -> None:
        markup = render_panels(parse_decision(copy.deepcopy(VALID)))

        self.assertIn("What was broken", markup)
        self.assertIn("Is it actually fixed", markup)
        self.assertIn("machine-checked", markup)

    def test_agent_text_is_escaped(self) -> None:
        document = copy.deepcopy(VALID)
        document["gaps"][0]["gap"] = "<script>alert(1)</script>"

        markup = render_gaps(parse_decision(document))

        self.assertNotIn("<script>", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_questions_are_numbered_and_lettered(self) -> None:
        markup = render_questions(parse_decision(copy.deepcopy(VALID)), first_number=4)

        self.assertIn(">4<", markup)
        self.assertIn(">A<", markup)
        self.assertIn(">B<", markup)



class DecisionGateCliTests(unittest.TestCase):
    """The exit code is the gate. A person cannot review a pip command list."""

    def _run(self, data_root: Path) -> str:
        from mailman.artifacts import create_run

        run, _ = create_run(
            repository="https://github.com/example/project.git",
            issue="https://github.com/example/project/issues/7",
            base_commit="a" * 40,
            primary="codex",
            reviewer="claude",
            data_root=data_root,
        )
        return run.run_id

    def test_review_exits_non_zero_without_a_decision(self) -> None:
        from mailman.cli import main

        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run_id = self._run(data_root)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                exit_code = main(
                    ["review", run_id, "--no-open", "--data-root", str(data_root)]
                )

        self.assertEqual(exit_code, 1)

    def test_init_then_check_walks_a_model_to_a_valid_file(self) -> None:
        from mailman.cli import main

        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run_id = self._run(data_root)
            arguments = ["decision", run_id, "--data-root", str(data_root)]
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                initialized = main(arguments + ["--init"])
                unfilled = main(arguments)
            path = data_root / run_id / DECISION_FILENAME
            path.write_text(json.dumps(VALID), encoding="utf-8")
            with redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()):
                filled = main(arguments)

        self.assertEqual(initialized, 0)
        self.assertEqual(unfilled, 1)
        self.assertEqual(filled, 0)
        self.assertEqual(json.loads(stdout.getvalue())["blocking"], 1)

    def test_init_refuses_to_overwrite_a_filled_file(self) -> None:
        from mailman.cli import main

        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "runs"
            run_id = self._run(data_root)
            (data_root / run_id / DECISION_FILENAME).write_text(
                json.dumps(VALID), encoding="utf-8"
            )
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                exit_code = main(
                    ["decision", run_id, "--init", "--data-root", str(data_root)]
                )
            still_there = json.loads(
                (data_root / run_id / DECISION_FILENAME).read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 2)
        self.assertEqual(still_there["recommendation"], "SEND")


if __name__ == "__main__":
    unittest.main()
