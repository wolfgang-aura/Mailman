"""The decision layer of a review page: what a person is being asked to decide.

`review_page` renders the evidence of a run. Evidence is not a decision. Left
to itself, every model that has prepared a run for review has written the
provenance first and buried the question, or skipped the question entirely and
handed over a page of links. This module fixes the part that cannot be derived
from the run directory -- the three answers, the questions, the open gaps --
into a file an agent writes and a validator refuses when it is wrong.

The schema is deliberately hostile. A question that is not a question, an
option with no cost, a gap with no reason and no price: each is rejected with a
message naming the fix. A model that cannot satisfy the schema has not finished
thinking about the run, and the page it would have written is worse than none.

See docs/review-page-standard.md for the format itself and why it is ordered
the way it is.
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DECISION_FILENAME = "decision.json"
DECISION_SCHEMA_VERSION = 1

#: The three panels, in the order a person reads them. Not configurable.
PANEL_KEYS = ("broken", "did", "fixed")
PANEL_TITLES = {
    "broken": "What was broken",
    "did": "What we did",
    "fixed": "Is it actually fixed",
}

#: How a claim is known. Kept apart on the page, because "the tests pass" and
#: "the agent says it works" are not the same sentence and must never be
#: rendered as though they were.
EVIDENCE_CLASSES = (
    "machine-checked",
    "measured",
    "seen",
    "deployed",
    "live-verified",
    "agent-claimed",
    "reasoned",
    "unverified",
)
_MACHINE_CLASSES = {"machine-checked", "measured", "seen", "deployed", "live-verified"}

#: What the page recommends the human do with the patch.
RECOMMENDATIONS = ("SEND", "HOLD", "DROP")
_RECOMMENDATION_TONE = {"SEND": "ok", "HOLD": "warn", "DROP": "stop"}

#: Options are labelled so the operator answers "1A 2B 3A" instead of retyping
#: the choice he is picking.
OPTION_LABELS = "ABCDEFGH"

_MAX_CLAIM = 220


class DecisionError(ValueError):
    """A decision file that cannot be rendered, with every problem listed."""

    def __init__(self, problems: list[str], path: Path | None = None) -> None:
        self.problems = problems
        self.path = path
        where = f" in {path}" if path is not None else ""
        joined = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(f"{len(problems)} problem(s){where}:\n{joined}")


@dataclass(frozen=True)
class Panel:
    key: str
    claim: str
    detail: str
    evidence: str


@dataclass(frozen=True)
class Option:
    label: str
    text: str
    cost: str


@dataclass(frozen=True)
class Question:
    question: str
    options: list[Option]
    recommendation: str
    blocking: bool


@dataclass(frozen=True)
class Gap:
    gap: str
    why_open: str
    cost_to_close: str


@dataclass(frozen=True)
class LedgerEntry:
    claim: str
    kind: str
    evidence: str


@dataclass(frozen=True)
class Decision:
    """One run's answer to "what am I being asked, and on what evidence"."""

    recommendation: str
    headline: str
    panels: list[Panel]
    questions: list[Question] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)

    @property
    def blocking_questions(self) -> list[Question]:
        return [question for question in self.questions if question.blocking]


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, (str, int, float)) else ""


def _claim_problem(where: str, value: str) -> str | None:
    if not value:
        return f"{where} is empty; write the sentence."
    if "\n" in value:
        return f"{where} is more than one sentence; move the rest into the detail."
    if len(value) > _MAX_CLAIM:
        return f"{where} is {len(value)} characters; keep it under {_MAX_CLAIM}."
    return None


def _parse_panels(raw_panels: Any, problems: list[str]) -> list[Panel]:
    panels: list[Panel] = []
    if not isinstance(raw_panels, dict):
        problems.append("panels is missing; all three panels are required.")
        raw_panels = {}
    for key in PANEL_KEYS:
        entry = raw_panels.get(key)
        if not isinstance(entry, dict):
            problems.append(f"panels.{key} is missing ({PANEL_TITLES[key]}).")
            continue
        claim = _text(entry.get("claim"))
        detail = _text(entry.get("detail"))
        evidence = _text(entry.get("evidence"))
        problem = _claim_problem(f"panels.{key}.claim", claim)
        if problem:
            problems.append(problem)
        if not detail:
            problems.append(
                f"panels.{key}.detail is empty; one supporting sentence is required."
            )
        if evidence not in EVIDENCE_CLASSES:
            problems.append(
                f"panels.{key}.evidence is {entry.get('evidence')!r}; use one of "
                f"{', '.join(EVIDENCE_CLASSES)}."
            )
        panels.append(Panel(key, claim, detail, evidence))
    return panels


def _parse_options(where: str, raw_options: Any, problems: list[str]) -> list[Option]:
    options: list[Option] = []
    if not isinstance(raw_options, list) or len(raw_options) < 2:
        problems.append(f"{where}.options needs at least two concrete options.")
        return options
    if len(raw_options) > len(OPTION_LABELS):
        problems.append(
            f"{where}.options has {len(raw_options)} entries; at most "
            f"{len(OPTION_LABELS)}."
        )
        raw_options = raw_options[: len(OPTION_LABELS)]
    for position, option in enumerate(raw_options):
        expected = OPTION_LABELS[position]
        if not isinstance(option, dict):
            problems.append(f"{where}.options[{position + 1}] is not an object.")
            continue
        label = _text(option.get("label")).upper() or expected
        if label != expected:
            problems.append(
                f"{where}.options[{position + 1}] is labelled {label!r}; labels run "
                f"A, B, C in order, so this one is {expected!r}."
            )
            label = expected
        option_text = _text(option.get("text"))
        cost = _text(option.get("cost"))
        if not option_text:
            problems.append(f"{where}.options.{label}.text is empty.")
        if not cost:
            problems.append(
                f"{where}.options.{label}.cost is empty; every option costs "
                "something, and the cost is why he is choosing."
            )
        options.append(Option(label, option_text, cost))
    return options


def _parse_questions(raw_questions: Any, problems: list[str]) -> list[Question]:
    questions: list[Question] = []
    if raw_questions is None:
        problems.append(
            "questions is missing; write [] to state that nothing is blocked on him."
        )
        return questions
    if not isinstance(raw_questions, list):
        problems.append("questions is not a list.")
        return questions
    for index, entry in enumerate(raw_questions, start=1):
        where = f"questions[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} is not an object.")
            continue
        text = _text(entry.get("question"))
        if not text:
            problems.append(f"{where}.question is empty.")
        elif not text.endswith("?"):
            problems.append(
                f"{where}.question does not end in a question mark; a decision is "
                "asked, never stated."
            )
        options = _parse_options(where, entry.get("options"), problems)
        recommendation = _text(entry.get("recommendation"))
        labels = [option.label for option in options]
        if not recommendation:
            problems.append(f"{where}.recommendation is empty; recommend one option.")
        elif labels and recommendation[:1].upper() not in labels:
            problems.append(
                f"{where}.recommendation does not start with an option label; write "
                f'e.g. "{labels[0]} - because ...".'
            )
        blocking = entry.get("blocking")
        if not isinstance(blocking, bool):
            problems.append(
                f"{where}.blocking is {blocking!r}; say true or false, so he knows "
                "what stops the patch."
            )
            blocking = bool(blocking)
        questions.append(Question(text, options, recommendation, blocking))
    return questions


def _parse_gaps(raw_gaps: Any, problems: list[str]) -> list[Gap]:
    gaps: list[Gap] = []
    if raw_gaps is None:
        problems.append("gaps is missing; write [] when nothing is left open.")
        return gaps
    if not isinstance(raw_gaps, list):
        problems.append("gaps is not a list.")
        return gaps
    for index, entry in enumerate(raw_gaps, start=1):
        where = f"gaps[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} is not an object.")
            continue
        gap = _text(entry.get("gap"))
        why_open = _text(entry.get("why_open"))
        cost_to_close = _text(entry.get("cost_to_close"))
        if not gap:
            problems.append(f"{where}.gap is empty.")
        if not why_open:
            problems.append(
                f"{where}.why_open is empty; an unchecked item that does not say why "
                "it is unchecked is work you skipped, not a gap."
            )
        if not cost_to_close:
            problems.append(
                f"{where}.cost_to_close is empty; say what closing it would take, in "
                "time, tooling or access."
            )
        gaps.append(Gap(gap, why_open, cost_to_close))
    return gaps


def _parse_ledger(raw_ledger: Any, problems: list[str]) -> list[LedgerEntry]:
    ledger: list[LedgerEntry] = []
    if not isinstance(raw_ledger, list) or not raw_ledger:
        problems.append(
            "ledger is missing or empty; every claim on the page is listed with how "
            "it is known."
        )
        return ledger
    for index, entry in enumerate(raw_ledger, start=1):
        where = f"ledger[{index}]"
        if not isinstance(entry, dict):
            problems.append(f"{where} is not an object.")
            continue
        claim = _text(entry.get("claim"))
        kind = _text(entry.get("kind"))
        evidence = _text(entry.get("evidence"))
        if not claim:
            problems.append(f"{where}.claim is empty.")
        if kind not in EVIDENCE_CLASSES:
            problems.append(
                f"{where}.kind is {entry.get('kind')!r}; use one of "
                f"{', '.join(EVIDENCE_CLASSES)}."
            )
        if not evidence:
            problems.append(
                f"{where}.evidence is empty; name the command, the file or the "
                "capture that carries the claim."
            )
        ledger.append(LedgerEntry(claim, kind, evidence))
    return ledger


def parse_decision(data: Any) -> Decision:
    """Turn a decision document into a `Decision`, or say everything wrong with it."""
    if not isinstance(data, dict):
        raise DecisionError(["the decision file is not a JSON object."])
    problems: list[str] = []

    version = data.get("schema_version")
    if version != DECISION_SCHEMA_VERSION:
        problems.append(
            f"schema_version is {version!r}; this Mailman writes and reads "
            f"{DECISION_SCHEMA_VERSION}."
        )

    recommendation = _text(data.get("recommendation")).upper()
    if recommendation not in RECOMMENDATIONS:
        problems.append(
            f"recommendation is {data.get('recommendation')!r}; use one of "
            f"{', '.join(RECOMMENDATIONS)}."
        )

    headline = _text(data.get("headline"))
    problem = _claim_problem("headline", headline)
    if problem:
        problems.append(problem)

    panels = _parse_panels(data.get("panels"), problems)
    questions = _parse_questions(data.get("questions"), problems)
    gaps = _parse_gaps(data.get("gaps"), problems)
    ledger = _parse_ledger(data.get("ledger"), problems)

    if problems:
        raise DecisionError(problems)
    return Decision(recommendation, headline, panels, questions, gaps, ledger)


def load_decision(run_directory: Path) -> Decision:
    """Read and validate the run's decision file."""
    path = Path(run_directory) / DECISION_FILENAME
    if not path.is_file():
        raise DecisionError(
            [
                f"{DECISION_FILENAME} does not exist. A review page without it is "
                "evidence with no question attached; write the file first."
            ],
            path,
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise DecisionError([f"not valid JSON: {error}"], path) from error
    try:
        return parse_decision(data)
    except DecisionError as error:
        raise DecisionError(error.problems, path) from None


def blank_decision() -> dict[str, Any]:
    """A skeleton an agent fills in. Every string here fails validation on purpose."""
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "recommendation": "HOLD",
        "headline": "",
        "panels": {
            key: {"claim": "", "detail": "", "evidence": "machine-checked"}
            for key in PANEL_KEYS
        },
        "questions": [
            {
                "question": "",
                "blocking": True,
                "options": [
                    {"label": "A", "text": "", "cost": ""},
                    {"label": "B", "text": "", "cost": ""},
                ],
                "recommendation": "",
            }
        ],
        "gaps": [],
        "ledger": [{"claim": "", "kind": "machine-checked", "evidence": ""}],
    }


# --- rendering -------------------------------------------------------------
#
# The markup below is the fixed presentation. It reuses the tokens and the
# component vocabulary in DESIGN.md; nothing here introduces a colour, a font
# size or a spacing step that is not already there.


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _stamp(evidence: str) -> str:
    tone = "ok" if evidence in _MACHINE_CLASSES else "warn"
    return f'<span class="pill {tone}" title="how this is known">{_escape(evidence)}</span>'


def recommendation_pill(recommendation: str) -> str:
    tone = _RECOMMENDATION_TONE.get(recommendation, "warn")
    return f'<span class="pill {tone}">{_escape(recommendation)}</span>'


def render_panels(decision: Decision) -> str:
    """Three answers above the fold, in the order a person asks them."""
    cells = []
    for panel in decision.panels:
        cells.append(
            f'<div class="panel"><span class="label">{_escape(PANEL_TITLES[panel.key])}'
            f'</span><p class="claim">{_escape(panel.claim)}</p>'
            f'<p class="detail">{_escape(panel.detail)}</p>'
            f"{_stamp(panel.evidence)}</div>"
        )
    return f'<div class="panels">{"".join(cells)}</div>'


def render_questions(decision: Decision, first_number: int = 1) -> str:
    """What he has to answer, as questions with priced options and a pick."""
    if not decision.questions:
        return (
            '<p class="note">Nothing is blocked on you. The recommendation stands on '
            "the evidence below.</p>"
        )
    blocks = []
    for offset, question in enumerate(decision.questions):
        number = first_number + offset
        rows = "".join(
            f'<tr><td class="optlabel">{_escape(option.label)}</td>'
            f"<td>{_escape(option.text)}</td>"
            f'<td class="cost">{_escape(option.cost)}</td></tr>'
            for option in question.options
        )
        mark = (
            '<span class="pill stop">blocks</span>'
            if question.blocking
            else '<span class="pill warn">does not block</span>'
        )
        blocks.append(
            f'<div class="card question"><header><span class="qnum">{number}</span>'
            f"<h3>{_escape(question.question)}</h3>{mark}</header>"
            '<table class="options"><thead><tr><th>#</th><th>Option</th>'
            f"<th>What it costs</th></tr></thead><tbody>{rows}</tbody></table>"
            f'<p class="pick"><span class="label">Recommendation</span>'
            f"{_escape(question.recommendation)}</p></div>"
        )
    return "".join(blocks)


def render_gaps(decision: Decision) -> str:
    """Open items, each with the reason it is open and the price of closing it."""
    if not decision.gaps:
        return (
            '<p class="note">No gap is open. Nothing was left unchecked without a '
            "reason.</p>"
        )
    rows = "".join(
        f"<tr><td><strong>{_escape(gap.gap)}</strong></td>"
        f"<td>{_escape(gap.why_open)}</td><td>{_escape(gap.cost_to_close)}</td></tr>"
        for gap in decision.gaps
    )
    return (
        '<div class="card scroller"><table class="grid"><thead><tr>'
        "<th>Gap</th><th>Why it is still open</th><th>What would close it</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def render_ledger(decision: Decision) -> str:
    """Every claim on the page with the class of evidence behind it."""
    rows = "".join(
        f"<tr><td>{_escape(entry.claim)}</td><td>{_stamp(entry.kind)}</td>"
        f'<td class="mono">{_escape(entry.evidence)}</td></tr>'
        for entry in decision.ledger
    )
    return (
        '<div class="card scroller"><table class="grid"><thead><tr>'
        "<th>Claim</th><th>How it is known</th><th>Evidence</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def render_missing(error: DecisionError) -> str:
    """The page a run gets when its decision file is absent or wrong.

    Loud on purpose. A silently degraded review page is the failure this whole
    module exists to prevent, so it names the file, the problems and the fix.
    """
    items = "".join(f"<li>{_escape(problem)}</li>" for problem in error.problems)
    return (
        '<div class="card missing"><p class="byline">This page is incomplete</p>'
        '<p class="claim">No usable decision has been written for this run.</p>'
        "<p class=\"detail\">A review page carries evidence. Without a valid "
        f"<code>{DECISION_FILENAME}</code> it carries no question, and there is "
        "nothing here for a person to answer.</p>"
        f'<ul class="problems">{items}</ul>'
        '<p class="detail">Write the file with <code>mailman decision RUN_ID '
        "--init</code>, fill it in, then render the page again.</p></div>"
    )
