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
import re
from dataclasses import dataclass, field, replace
from collections.abc import Iterable
from pathlib import Path
from typing import Any

DECISION_FILENAME = "decision.json"
DECISION_SCHEMA_VERSION = 1
# The body `decision --body/--affirm` checked and the lines it affirmed, so
# finalize-review and hunt status read the same gate package passed (#329).
AFFIRMATIONS_FILENAME = "affirmations.json"

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

#: What the page recommends the human do with the patch. ASK is ask-first: the
#: candidate is verified, but nobody who maintains the project has said the
#: behaviour is a bug, so the operator approves a short offer comment on the
#: issue and the pull request waits for a maintainer's answer.
#: https://github.com/wolfgang-aura/Mailman/issues/138
RECOMMENDATIONS = ("SEND", "ASK", "HOLD", "DROP")
_RECOMMENDATION_TONE = {"SEND": "ok", "ASK": "warn", "HOLD": "warn", "DROP": "stop"}

#: An offer comment is a question to a maintainer, not a pull request body.
#: The two that worked (edgartools#1337, #1370) were under a hundred words.
OFFER_WORD_LIMIT = 120
_REPRODUCTION_WORD = re.compile(r"\brepro(?:duc\w*)?\b", re.IGNORECASE)
_COMMIT_WORD = re.compile(r"\b[0-9a-f]{7,40}\b")

#: Options are labelled so the operator answers "1A 2B 3A" instead of retyping
#: the choice he is picking.
OPTION_LABELS = "ABCDEFGH"

from mailman.claims import load_claims, triage_warning
from mailman.target_intel import load_target_intel

UNTRIAGED_GATE = "untriaged-issue"
#: A question the operator answers by signing the target's Contributor License
#: Agreement. Signing happens at filing approval, like the own-words rewrite,
#: so it is not coordinator work. Mailman #290.
CLA_GATE = "cla"
#: A question the operator answers by rewriting the body in their own words,
#: which also happens at filing approval. It is set aside only on a run
#: whose sole submission hold is the own-words policy. Mailman #181.
OWN_WORDS_GATE = "own-words"
#: A question the operator answers by committing to reply to review comments
#: personally, which a target's AI policy may require. Like the CLA, the
#: commitment is made at filing approval. Mailman #381.
PERSONAL_REVIEW_GATE = "personal-review"

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
    gate: str | None = None


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
class Offer:
    """The comment an ASK decision proposes for the issue. Mailman never posts it."""

    path: str
    text: str = ""


@dataclass(frozen=True)
class Decision:
    """One run's answer to "what am I being asked, and on what evidence"."""

    recommendation: str
    headline: str
    panels: list[Panel]
    questions: list[Question] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    ledger: list[LedgerEntry] = field(default_factory=list)
    offer: Offer | None = None

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
        gate = entry.get("gate")
        if gate is not None and not isinstance(gate, str):
            problems.append(f"{where}.gate is {gate!r}; a gate is named by a string.")
            gate = None
        questions.append(Question(text, options, recommendation, blocking, gate))
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


def _parse_offer(raw: Any, recommendation: str, problems: list[str]) -> Offer | None:
    if raw is None:
        if recommendation == "ASK":
            problems.append(
                'recommendation ASK needs an offer block, e.g. {"path": '
                '"offer-comment.md"}: the comment draft in the run directory the '
                "operator approves before anything is posted."
            )
        return None
    path = _text(raw.get("path")) if isinstance(raw, dict) else ""
    if not path:
        problems.append(
            "offer.path is empty; name the comment draft in the run directory."
        )
        return None
    if recommendation != "ASK":
        problems.append(
            f"offer belongs to an ASK recommendation, not {recommendation or 'none'}; "
            "remove it once a maintainer has answered and the run moves on."
        )
    return Offer(path)


def _not_utf8(path: Path, error: UnicodeDecodeError) -> str:
    """The problem for a file the decision reads that is not UTF-8 (#337)."""
    return (
        f"{path.name} is not valid UTF-8 ({error}). Re-save it as UTF-8; "
        "Windows PowerShell 5.1 writes UTF-16 by default."
    )


def offer_problems(run_directory: Path, offer: Offer) -> tuple[list[str], str]:
    """What is wrong with the offer draft on disk, and its text when nothing is."""
    root = Path(run_directory).resolve()
    draft = (root / offer.path).resolve()
    if not draft.is_relative_to(root):
        return [f"offer.path {offer.path!r} must stay inside the run directory."], ""
    if not draft.is_file():
        return [f"offer.path {offer.path!r} does not exist; write the draft first."], ""
    try:
        text = draft.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError as error:
        return [_not_utf8(draft, error)], ""
    problems: list[str] = []
    words = len(text.split())
    if not words:
        problems.append(f"{offer.path} is empty.")
    elif words >= OFFER_WORD_LIMIT:
        problems.append(
            f"{offer.path} is {words} words; keep an offer under "
            f"{OFFER_WORD_LIMIT}. It asks whether a pull request is wanted; the "
            "pull request says the rest."
        )
    if not _REPRODUCTION_WORD.search(text):
        problems.append(
            f"{offer.path} does not name the reproduction; say that it reproduces "
            "and with what."
        )
    try:
        run = json.loads((root / "run.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        run = {}
    base = str(run.get("base_commit") or "").lower() if isinstance(run, dict) else ""
    if not base:
        problems.append("run.json names no base commit for the offer to cite.")
    elif not any(base.startswith(word) for word in _COMMIT_WORD.findall(text.lower())):
        problems.append(
            f"{offer.path} does not name the base commit; cite {base[:8]} so the "
            "maintainer can check the reproduction against the same tree."
        )
    return problems, text


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
    offer = _parse_offer(data.get("offer"), recommendation, problems)

    if problems:
        raise DecisionError(problems)
    return Decision(recommendation, headline, panels, questions, gaps, ledger, offer)


def _affirmed_claims(body: Path, lines: Iterable[int]) -> list[dict[str, Any]]:
    """The claims on `lines` of `body`, refused as `handoff --affirm` refuses them."""
    from mailman.handoff import _split_affirmed, first_person_claims  # handoff imports this module

    wanted = sorted(set(lines))
    if not wanted:
        return []
    try:
        text = body.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(_not_utf8(body, error)) from error
    except OSError as error:
        raise ValueError(f"--affirm needs the body, and {body} cannot be read ({error})") from error
    return _split_affirmed(first_person_claims(text), wanted)[1]


def record_affirmations(
    run_directory: Path, *, body_path: Path, lines: Iterable[int]
) -> Path:
    """Record the body the decision gate checked and the lines affirmed in it.

    `package --affirm` passed the decision stage and then stopped at
    finalize-review, which read the decision with no affirmations. Each claim
    is kept by its text, so an edit to the line voids it. Mailman #329.
    """
    body = Path(body_path).resolve()
    claims = _affirmed_claims(body, lines)
    # A rerun without --affirm keeps what was affirmed in the same body; a
    # claim is kept by its text, so an edited line still drops out.
    earlier = _recorded_affirmations(Path(run_directory))
    if earlier.get("body_path") == str(body):
        texts = {claim["text"] for claim in claims}
        claims += [
            claim
            for claim in earlier.get("affirmed_claims") or []
            if isinstance(claim, dict) and claim.get("text") not in texts
        ]
    record = {
        "schema_version": 1,
        "body_path": str(body),
        "affirmed_claims": claims,
    }
    path = Path(run_directory) / AFFIRMATIONS_FILENAME
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def _recorded_affirmations(run_directory: Path) -> dict[str, Any]:
    path = run_directory / AFFIRMATIONS_FILENAME
    if not path.is_file():
        return {}
    again = "run `mailman decision RUN_ID --affirm LINE` again."
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DecisionError([f"{AFFIRMATIONS_FILENAME} cannot be read ({error}); {again}"], path) from None
    if not isinstance(record, dict):
        raise DecisionError([f"{AFFIRMATIONS_FILENAME} is not an affirmation record; {again}"], path)
    return record


def load_decision(
    run_directory: Path,
    *,
    affirmed_lines: Iterable[int] = (),
    body_path: Path | None = None,
) -> Decision:
    """Read and validate the run's decision file.

    `affirmed_lines` are body lines the operator affirms before handoff.json
    exists, as `package --affirm` does (#217). Without `body_path`, the body
    and affirmations `decision` recorded are read, then the run's body.md.
    """
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
    except UnicodeDecodeError as error:
        raise DecisionError([_not_utf8(path, error)], path) from error
    except json.JSONDecodeError as error:
        raise DecisionError([f"not valid JSON: {error}"], path) from error
    try:
        decision = parse_decision(data)
    except DecisionError as error:
        raise DecisionError(error.problems, path) from None
    recorded = _recorded_affirmations(Path(run_directory))
    body = Path(body_path or recorded.get("body_path") or Path(run_directory) / "body.md")
    affirmed = {
        claim.get("text")
        for claim in recorded.get("affirmed_claims") or []
        if isinstance(claim, dict)
    }
    try:
        affirmed |= {claim["text"] for claim in _affirmed_claims(body, affirmed_lines)}
    except ValueError as error:
        raise DecisionError([str(error)], path) from None
    problem = (
        untriaged_problem(Path(run_directory), decision)
        or assignment_problem(Path(run_directory), decision)
        or body_claim_problem(
            Path(run_directory), decision, body=body, affirmed=affirmed
        )
    )
    if problem:
        raise DecisionError([problem], path)
    if decision.offer is not None:
        problems, text = offer_problems(Path(run_directory), decision.offer)
        if problems:
            raise DecisionError(problems, path)
        decision = replace(decision, offer=replace(decision.offer, text=text))
    return decision


def body_claim_problem(
    run_directory: Path,
    decision: Decision,
    *,
    body: Path | None = None,
    affirmed: Iterable[str] = (),
) -> str | None:
    """Why SEND cannot stand: body.md says something only the human can make true.

    The spack run passed with SEND, the operator packaged it, and only then did
    handoff refuse "I have read and tested every line". Mailman #294. A line
    the operator affirms counts, whether handoff.json recorded it or the
    affirmation came with `package --affirm`, which validates the decision
    before handoff.json exists.
    """
    body = body or run_directory / "body.md"
    if decision.recommendation != "SEND" or not body.is_file():
        return None
    from mailman.handoff import first_person_claims  # handoff imports this module

    handoff = run_directory / "handoff.json"
    affirmed = set(affirmed)
    if handoff.is_file():
        try:
            record = json.loads(handoff.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            return f"handoff.json cannot be read ({error}); run `mailman handoff` again."
        if not isinstance(record, dict):
            return "handoff.json is not a handoff record; run `mailman handoff` again."
        affirmed |= {
            claim.get("text")
            for claim in record.get("affirmed_claims") or []
            if isinstance(claim, dict)
        }
    try:
        text = body.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        return _not_utf8(body, error)
    claims = [
        claim
        for claim in first_person_claims(text)
        if claim["text"] not in affirmed
    ]
    if not claims:
        return None
    where = ", ".join(f"{body.name} line {claim['line']}" for claim in claims)
    return (
        f"{where} makes a claim only the human filing it can make true; "
        "handoff will refuse it. Remove it, or recommend something other than SEND."
    )


def assignment_problem(run_directory: Path, decision: Decision) -> str | None:
    """Why SEND cannot stand: the target merges only assigned work and nobody holds this issue.

    semantica#1846: target-intel said every outside merge held the issue's
    assignment first and CONTRIBUTING said to wait for it, yet SEND validated
    on an unassigned issue. Mailman #399. Not seeded as a question: a blocking
    question would also stop the ASK path, which is the way through.
    """
    if decision.recommendation != "SEND":
        return None
    assessment = (load_target_intel(run_directory) or {}).get("assessment") or {}
    if not assessment.get("assignment_looks_required"):
        return None
    claims = load_claims(run_directory) or {}
    if claims.get("assignees"):
        return None
    held = assessment.get("merges_whose_author_held_the_assignment", 0)
    read = assessment.get("merge_path_rows_read", 0)
    return (
        f"target-intel: {held} of {read} outside merge(s) read held the linked "
        "issue's assignment first, and this issue has no assignee. A pull request "
        "opened now is closed as unassigned. Recommend ASK with an offer comment "
        "asking to be assigned, or HOLD."
    )


def untriaged_problem(run_directory: Path, decision: Decision) -> str | None:
    """Why this decision cannot stand: the untriaged warning fired and nobody was asked.

    `handoff` has printed UNTRIAGED ISSUE since pytest#14993. It printed it for
    skfolio#316 too, to a coordinator's terminal; the review page said zero
    questions, the operator approved, and the maintainer closed the pull
    request in fifteen minutes because the behaviour was a choice. The warning
    belongs on the page, as the question it is.
    """
    warning = triage_warning(run_directory)
    if warning is None:
        return None
    # Blocking, or the run counts as ready with the question unanswered (#333).
    if any(
        question.gate == UNTRIAGED_GATE and question.blocking
        for question in decision.questions
    ):
        return None
    return (
        f"the claims record says {warning} Nobody who decides has been asked. "
        f"Keep the question `mailman decision --init` seeds (gate "
        f"{UNTRIAGED_GATE!r}, blocking true), or write one with that gate that "
        "is blocking."
    )


def untriaged_question(warning: str) -> dict[str, Any]:
    """The question the operator answers before filing on an unanswered outside report."""
    return {
        "question": (
            "No maintainer has said this is a bug: " + warning.split(". ")[0]
            + ". File the pull request anyway?"
        ),
        "blocking": True,
        "gate": UNTRIAGED_GATE,
        "options": [
            {
                "label": "A",
                "text": "Ask on the issue first and file once a maintainer answers.",
                "cost": "Days of delay; the patch may go stale.",
            },
            {
                "label": "B",
                "text": "File now.",
                "cost": (
                    "pytest#14993 and skfolio#316 were filed in this state and closed "
                    "as not-a-bug; a closed pull request under the account's name."
                ),
            },
            {
                "label": "C",
                "text": "Drop the target.",
                "cost": "The work done on the run.",
            },
        ],
        "recommendation": "A - a reproduction proves the behaviour, not that it is unwanted.",
    }


TOOL_COMPARISON_GATE = "tool-comparison"


def tool_comparison_question(comparisons: list[dict[str, Any]]) -> dict[str, Any]:
    """Ask whether the change goes against what the thread says other tools do.

    Non-blocking: the sentence may support the change as easily as oppose it,
    and only a person reading it can tell. pypdf#4035's thread said poppler,
    mutool and pdf.js all behaved one way. Mailman #124.
    """
    quoted = "; ".join(f'"{entry.get("quote")}"' for entry in comparisons[:3])
    return {
        "question": (
            f"The issue thread compares other tools: {quoted}. Does the change "
            "match what they do?"
        ),
        "blocking": False,
        "gate": TOOL_COMPARISON_GATE,
        "options": [
            {
                "label": "A",
                "text": "It matches them; say so in the pull request body.",
                "cost": "One sentence of evidence in the description.",
            },
            {
                "label": "B",
                "text": "It departs from them; ask on the issue before filing.",
                "cost": "Days of delay while a maintainer answers.",
            },
            {
                "label": "C",
                "text": "Drop the target.",
                "cost": "The work done on the run.",
            },
        ],
        "recommendation": (
            "A - if the reproduction shows the change matches them; otherwise B."
        ),
    }


def blank_decision(run_directory: Path | None = None) -> dict[str, Any]:
    """A skeleton an agent fills in. Every string here fails validation on purpose.

    When the run's claims record shows an unanswered outside report, the
    untriaged question is seeded complete; it is the one question the agent
    may not remove.
    """
    warning = triage_warning(run_directory) if run_directory is not None else None
    seeded = [untriaged_question(warning)] if warning else []
    intel = load_target_intel(run_directory) if run_directory is not None else None
    comparisons = (intel or {}).get("tool_comparisons") or []
    if comparisons:
        seeded.append(tool_comparison_question(comparisons))
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "recommendation": "HOLD",
        "headline": "",
        "panels": {
            key: {"claim": "", "detail": "", "evidence": "machine-checked"}
            for key in PANEL_KEYS
        },
        "questions": seeded
        + [
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


def render_offer(decision: Decision, where: str = "") -> str:
    """The offer comment an ASK decision wants approved, verbatim and escaped."""
    if decision.offer is None:
        return ""
    return (
        f'<div class="card"><p class="byline">Offer comment{_escape(where)} '
        f'&middot; <span class="mono">{_escape(decision.offer.path)}</span></p>'
        f'<pre class="block">{_escape(decision.offer.text)}</pre>'
        '<p class="note" style="margin:12px 16px">Mailman never posts this. It is '
        "not a pull request: approve the comment, post it yourself, and file only "
        "after a maintainer answers.</p></div>"
    )


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
