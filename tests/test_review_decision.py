from __future__ import annotations

import copy
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from mailman.claims import CLAIMS_FILENAME
from mailman.review_decision import (
    DECISION_FILENAME,
    TOOL_COMPARISON_GATE,
    UNTRIAGED_GATE,
    DecisionError,
    blank_decision,
    load_decision,
    parse_decision,
    render_gaps,
    render_panels,
    render_questions,
)
from mailman.target_intel import TARGET_INTEL_FILENAME


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


class BodyClaimGateTests(unittest.TestCase):
    """spack run 20260930T180712Z-3af3d7: SEND passed, then handoff refused the body. #294."""

    def _decide(self, directory: Path, body: str) -> None:
        (directory / "body.md").write_text(body, encoding="utf-8")
        (directory / DECISION_FILENAME).write_text(json.dumps(VALID), encoding="utf-8")
        load_decision(directory)

    def test_send_refuses_a_body_that_claims_the_human_tested_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(DecisionError) as caught:
                self._decide(
                    Path(temporary_directory),
                    "Fixes the hash.\n\nI have read and tested every line and take "
                    "responsibility for it.\n",
                )

        problems = " ".join(caught.exception.problems)
        self.assertIn("body.md line 3", problems)

    def test_send_accepts_a_body_without_personal_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            self._decide(
                Path(temporary_directory),
                "Fixes the hash.\n\nWritten with Claude through the Mailman harness.\n",
            )

    def test_a_line_affirmed_before_handoff_passes(self) -> None:
        # biopython's template requires a first-person line (#217). package
        # validates the decision before handoff.json exists, so the
        # affirmation has to reach the gate directly.
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "body.md").write_text(
                "Fixes the hash.\n\nI have read the CONTRIBUTING file and ran pre-commit.\n",
                encoding="utf-8",
            )
            (directory / DECISION_FILENAME).write_text(json.dumps(VALID), encoding="utf-8")
            with self.assertRaises(DecisionError):
                load_decision(directory)
            load_decision(directory, affirmed_lines=[3])

    def test_a_malformed_handoff_is_a_decision_error(self) -> None:
        # A JSONDecodeError escaped load_decision, and review pages catch only
        # DecisionError, so one truncated file crashed the whole packet.
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / "handoff.json").write_text("{\"affirmed", encoding="utf-8")
            with self.assertRaises(DecisionError) as caught:
                self._decide(directory, "Fixes the hash.\n")
        self.assertIn("handoff.json", " ".join(caught.exception.problems))


def _write_untriaged_claims(directory: Path) -> None:
    (directory / CLAIMS_FILENAME).write_text(
        json.dumps({"reporter_association": "NONE", "maintainer_replied": False}),
        encoding="utf-8",
    )


class UntriagedIssueGateTests(unittest.TestCase):
    """skfolio#316: the warning went to a terminal; the page said zero questions."""

    def test_an_unanswered_outside_report_refuses_a_decision_that_never_asks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_untriaged_claims(directory)
            (directory / DECISION_FILENAME).write_text(
                json.dumps(VALID), encoding="utf-8"
            )
            with self.assertRaises(DecisionError) as caught:
                load_decision(directory)

        problems = " ".join(caught.exception.problems)
        self.assertIn("no owner, member or collaborator has replied", problems)
        self.assertIn(UNTRIAGED_GATE, problems)

    def test_the_seeded_question_satisfies_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            _write_untriaged_claims(directory)
            seeded = blank_decision(directory)["questions"][0]
            self.assertEqual(seeded["gate"], UNTRIAGED_GATE)
            self.assertTrue(seeded["blocking"])
            data = copy.deepcopy(VALID)
            data["questions"] = [seeded]
            (directory / DECISION_FILENAME).write_text(json.dumps(data), encoding="utf-8")
            decision = load_decision(directory)

        self.assertEqual(decision.questions[0].gate, UNTRIAGED_GATE)
        self.assertEqual(len(decision.blocking_questions), 1)

    def test_a_tool_comparison_seeds_a_non_blocking_question(self) -> None:
        quote = "Poppler, mutool and pdf.js all keep the first /Outlines entry."
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / TARGET_INTEL_FILENAME).write_text(
                json.dumps(
                    {
                        "success": True,
                        "tool_comparisons": [
                            {
                                "author": "stefan6419846",
                                "association": "MEMBER",
                                "tools": ["mutool", "pdf.js", "poppler"],
                                "quote": quote,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            seeded = blank_decision(directory)["questions"][0]
            self.assertEqual(seeded["gate"], TOOL_COMPARISON_GATE)
            self.assertFalse(seeded["blocking"])
            self.assertIn(quote, seeded["question"])
            data = copy.deepcopy(VALID)
            data["questions"] = [seeded]
            (directory / DECISION_FILENAME).write_text(json.dumps(data), encoding="utf-8")
            decision = load_decision(directory)

        self.assertEqual(decision.blocking_questions, [])

    def test_no_tool_comparison_seeds_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / TARGET_INTEL_FILENAME).write_text(
                json.dumps({"success": True, "tool_comparisons": None}),
                encoding="utf-8",
            )
            self.assertEqual(blank_decision(directory)["questions"][0]["question"], "")

    def test_a_maintainer_reply_seeds_nothing_and_gates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            (directory / CLAIMS_FILENAME).write_text(
                json.dumps({"reporter_association": "NONE", "maintainer_replied": True}),
                encoding="utf-8",
            )
            self.assertEqual(blank_decision(directory)["questions"][0]["question"], "")
            (directory / DECISION_FILENAME).write_text(
                json.dumps(VALID), encoding="utf-8"
            )
            load_decision(directory)


BASE_COMMIT = "b022ad29ac3965248c473b5790cf8e124887eac6"

OFFER_TEXT = (
    "@maintainer This still reproduces on `main` (b022ad29): the issue's "
    "snippet returns the wrong value.\n\nA fix is ready, with two regression "
    "tests. Would you like a PR, or would you rather fix it yourself?\n"
)


def ask_decision(directory: Path, *, offer_text: str | None = OFFER_TEXT,
                 offer: object = "default") -> dict:
    """An ASK decision on an untriaged run, with its offer draft on disk."""
    (directory / "run.json").write_text(
        json.dumps({"run_id": directory.name, "base_commit": BASE_COMMIT}),
        encoding="utf-8",
    )
    _write_untriaged_claims(directory)
    if offer_text is not None:
        (directory / "offer-comment.md").write_text(offer_text, encoding="utf-8")
    data = copy.deepcopy(VALID)
    data["recommendation"] = "ASK"
    data["questions"] = [blank_decision(directory)["questions"][0]]
    if offer == "default":
        data["offer"] = {"path": "offer-comment.md"}
    elif offer is not None:
        data["offer"] = offer
    (directory / DECISION_FILENAME).write_text(json.dumps(data), encoding="utf-8")
    return data


class AskFirstDecisionTests(unittest.TestCase):
    """https://github.com/wolfgang-aura/Mailman/issues/138

    Ask-first: a verified untriaged candidate is offered on the issue before
    any pull request. The offer draft is gated like the rest of the decision.
    """

    def load(self, **arguments: object):
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ask_decision(directory, **arguments)  # type: ignore[arg-type]
            return load_decision(directory)

    def problems(self, **arguments: object) -> str:
        with self.assertRaises(DecisionError) as caught:
            self.load(**arguments)
        return " ".join(caught.exception.problems)

    def test_an_ask_with_a_valid_offer_loads_and_carries_the_text(self) -> None:
        decision = self.load()

        self.assertEqual(decision.recommendation, "ASK")
        self.assertIsNotNone(decision.offer)
        self.assertEqual(decision.offer.path, "offer-comment.md")
        self.assertIn("b022ad29", decision.offer.text)

    def test_an_ask_without_an_offer_is_refused(self) -> None:
        self.assertIn("offer", self.problems(offer=None))

    def test_an_offer_file_that_does_not_exist_is_refused(self) -> None:
        self.assertIn("offer-comment.md", self.problems(offer_text=None))

    def test_an_offer_of_120_words_or_more_is_refused(self) -> None:
        long_text = OFFER_TEXT + " word" * 120
        self.assertIn("120", self.problems(offer_text=long_text))

    def test_an_offer_must_name_the_base_commit(self) -> None:
        text = OFFER_TEXT.replace("(b022ad29)", "(deadbeef1)")
        self.assertIn("base commit", self.problems(offer_text=text))

    def test_an_offer_must_name_the_reproduction(self) -> None:
        text = OFFER_TEXT.replace("still reproduces", "is still broken")
        self.assertIn("reproduction", self.problems(offer_text=text))

    def test_an_offer_outside_the_run_directory_is_refused(self) -> None:
        problems = self.problems(offer={"path": "../elsewhere/offer.md"})
        self.assertIn("inside the run directory", problems)

    def test_an_offer_on_a_send_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            data = ask_decision(directory)
            data["recommendation"] = "SEND"
            with self.assertRaises(DecisionError) as caught:
                parse_decision(data)
        self.assertIn("ASK", " ".join(caught.exception.problems))

    def test_the_offer_is_rendered_escaped(self) -> None:
        from mailman.review_decision import render_offer

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            ask_decision(directory, offer_text=OFFER_TEXT + "<script>x</script>\n")
            markup = render_offer(load_decision(directory))

        self.assertIn("Would you like a PR", markup)
        self.assertIn("&lt;script&gt;", markup)
        self.assertNotIn("<script>", markup)
        self.assertIn("never posts", markup)


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
