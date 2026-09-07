"""Many runs, one page, in the same language as a single run's review.

A batch is not a menu of links. When twenty-three patches are waiting, the
question is not "which page do I open first" but "how many of these need me,
and what am I being asked". So the packet leads with the roll-up, then the
questions across every run with one continuous numbering, so the whole batch
can be answered in a line like `1A 2B 3A`. Only after that does it list the
runs and link out to their pages.

Runs whose decision file is missing or invalid are not dropped from the packet.
They are listed as not ready, with the validator's own complaints, because a
run quietly missing from a batch page is how a patch gets forgotten.
"""

from __future__ import annotations

import html
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from mailman.review_decision import (
    Decision,
    DecisionError,
    load_decision,
    recommendation_pill,
)
from mailman.review_page import _read_json, _shorten
from mailman.review_page import _STYLE as _RUN_STYLE

_PACKET_STYLE = """
.packet { max-width: 1180px; margin: 0 auto; padding: 24px 16px 48px; }
.rollup { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-top: 16px; }
.rollup div { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.rollup .label { display: block; font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin-bottom: 4px; }
.rollup .value { font-size: 22px; font-weight: 600; font-family: "Cascadia Mono", Consolas, monospace; }
.runs td .runid { font-family: "Cascadia Mono", Consolas, monospace; font-size: 12px; color: var(--muted); display: block; }
.runs td.target { font-weight: 600; }
.asked { color: var(--muted); font-size: 13px; margin: 0 0 12px; }
"""


@dataclass(frozen=True)
class PacketEntry:
    """One run in a packet: either a decision, or the reason there is none."""

    run_directory: Path
    run_id: str
    target: str
    issue_url: str
    page_href: str
    decision: Decision | None
    error: DecisionError | None
    first_question: int = 0

    @property
    def ready(self) -> bool:
        return self.decision is not None


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _target_of(run: dict[str, Any], issue: dict[str, Any]) -> tuple[str, str]:
    """`owner/name#7` and the issue URL, the two things that name a run's work."""
    repository = str(run.get("repository") or "")
    repository = repository.removesuffix(".git").replace("https://github.com/", "")
    reference = issue.get("reference")
    number = ""
    url = ""
    if isinstance(reference, dict):
        number = str(reference.get("number") or "")
        url = str(reference.get("url") or "")
    label = f"{repository}#{number}" if repository and number else repository or "target"
    return label, url


def collect(
    run_directories: Sequence[Path],
    page_name: str = "review.html",
    link_base: Path | None = None,
) -> list[PacketEntry]:
    """Read every run once, numbering the questions across the whole batch.

    `link_base` is the directory the packet page will be written to, so a
    packet beside the runs and a packet in its own folder both link correctly.
    """
    entries: list[PacketEntry] = []
    next_number = 1
    for directory in run_directories:
        directory = Path(directory)
        run = _read_json(directory / "run.json")
        issue = _read_json(directory / "issue.json")
        run_id = str(run.get("run_id", directory.name))
        target, url = _target_of(run, issue)
        decision: Decision | None = None
        error: DecisionError | None = None
        try:
            decision = load_decision(directory)
        except DecisionError as problem:
            error = problem
        first = 0
        if decision is not None and decision.questions:
            first = next_number
            next_number += len(decision.questions)
        entries.append(
            PacketEntry(
                run_directory=directory,
                run_id=run_id,
                target=target,
                issue_url=url,
                page_href=_href(directory / page_name, link_base),
                decision=decision,
                error=error,
                first_question=first,
            )
        )
    return entries


def _href(page: Path, link_base: Path | None) -> str:
    """A relative link from where the packet will sit to a run's own page."""
    if link_base is None:
        return f"{page.parent.name}/{page.name}"
    return os.path.relpath(page, Path(link_base)).replace(os.sep, "/")


def _rollup(entries: Sequence[PacketEntry]) -> str:
    ready = [entry for entry in entries if entry.ready]
    blocking = sum(
        len(entry.decision.blocking_questions) for entry in ready if entry.decision
    )
    send = sum(1 for entry in ready if entry.decision and entry.decision.recommendation == "SEND")
    gaps = sum(len(entry.decision.gaps) for entry in ready if entry.decision)
    not_ready = len(entries) - len(ready)
    cells = [
        ("Runs waiting", str(len(entries))),
        ("Recommended to send", str(send)),
        ("Questions that block", str(blocking)),
        ("Open gaps", str(gaps)),
        ("No decision written", str(not_ready)),
    ]
    return '<div class="rollup">' + "".join(
        f'<div><span class="label">{_escape(label)}</span>'
        f'<span class="value">{_escape(value)}</span></div>'
        for label, value in cells
    ) + "</div>"


def _questions(entries: Sequence[PacketEntry]) -> str:
    """Every question in the batch, numbered once, answerable in one line."""
    blocks = []
    for entry in entries:
        if entry.decision is None or not entry.decision.questions:
            continue
        for offset, question in enumerate(entry.decision.questions):
            number = entry.first_question + offset
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
                f'<p class="asked"><a href="{_escape(entry.page_href)}">'
                f"{_escape(entry.target)}</a> &middot; "
                f'<span class="mono">{_escape(entry.run_id)}</span></p>'
                '<table class="options"><thead><tr><th>#</th><th>Option</th>'
                f"<th>What it costs</th></tr></thead><tbody>{rows}</tbody></table>"
                f'<p class="pick"><span class="label">Recommendation</span>'
                f"{_escape(question.recommendation)}</p></div>"
            )
    if not blocks:
        return (
            '<p class="note">No run in this batch is waiting on an answer. Every '
            "decision below is a straight yes or no on the patch.</p>"
        )
    return "".join(blocks)


def _runs_table(entries: Sequence[PacketEntry]) -> str:
    rows = []
    for entry in entries:
        decision = entry.decision
        if decision is None:
            continue
        numbers = ""
        if decision.questions:
            last = entry.first_question + len(decision.questions) - 1
            numbers = (
                str(entry.first_question)
                if last == entry.first_question
                else f"{entry.first_question}–{last}"
            )
        target = (
            f'<a href="{_escape(entry.issue_url)}">{_escape(entry.target)}</a>'
            if entry.issue_url
            else _escape(entry.target)
        )
        rows.append(
            f'<tr><td class="target">{target}'
            f'<span class="runid">{_escape(entry.run_id)}</span></td>'
            f"<td>{recommendation_pill(decision.recommendation)}</td>"
            f"<td>{_escape(_shorten(decision.headline, 160))}</td>"
            f'<td class="mono">{_escape(numbers or "none")}</td>'
            f'<td class="mono">{_escape(len(decision.gaps))}</td>'
            f'<td><a href="{_escape(entry.page_href)}">open</a></td></tr>'
        )
    if not rows:
        return '<p class="note">No run in this batch has a decision written.</p>'
    return (
        '<div class="card scroller"><table class="grid runs"><thead><tr>'
        "<th>Target</th><th>Recommendation</th><th>In one sentence</th>"
        "<th>Questions</th><th>Gaps</th><th>Page</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


def _not_ready(entries: Sequence[PacketEntry]) -> str:
    blocks = []
    for entry in entries:
        if entry.error is None:
            continue
        items = "".join(
            f"<li>{_escape(problem)}</li>" for problem in entry.error.problems
        )
        blocks.append(
            f'<div class="card missing"><p class="byline">No decision written</p>'
            f'<p class="claim">{_escape(entry.target)} '
            f'<span class="mono">{_escape(entry.run_id)}</span></p>'
            '<p class="detail">This run is in the batch but carries no usable '
            "decision, so it is not on the list above.</p>"
            f'<ul class="problems">{items}</ul></div>'
        )
    if not blocks:
        return ""
    return (
        "<section><h2>Not ready to decide</h2>"
        '<p class="note">Listed rather than dropped: a run missing from a batch '
        "page is a patch that gets forgotten.</p>"
        f"{''.join(blocks)}</section>"
    )


def render_packet_page(
    run_directories: Sequence[Path],
    title: str = "Runs waiting on a decision",
    page_name: str = "review.html",
    link_base: Path | None = None,
) -> str:
    """Render a batch of runs as one page in the fixed format."""
    entries = collect(run_directories, page_name=page_name, link_base=link_base)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(title)}</title>
<style>{_RUN_STYLE}{_PACKET_STYLE}</style></head>
<body><div class="packet">
<div class="masthead"><div><h1>{_escape(title)}</h1>
<p class="sub">Nothing here has been sent anywhere. Each run below is a patch
that goes upstream or does not, and the decision is yours.</p></div></div>
{_rollup(entries)}
<section><h2>What I need from you</h2>
<p class="note">Questions are numbered once across the whole batch. Answer by
number and letter &mdash; &ldquo;1A 2B 3A&rdquo; is a complete reply.</p>
{_questions(entries)}</section>
<section><h2>The runs</h2>
<p class="note">One row per run. Open a page for the patch, the evidence and
the gaps behind its recommendation.</p>
{_runs_table(entries)}</section>
{_not_ready(entries)}
</div></body></html>
"""


def write_packet_page(
    run_directories: Sequence[Path],
    destination: Path,
    title: str = "Runs waiting on a decision",
    page_name: str = "review.html",
) -> Path:
    """Write the packet page and return where it landed."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        render_packet_page(
            run_directories,
            title=title,
            page_name=page_name,
            link_base=destination.parent,
        ),
        encoding="utf-8",
    )
    return destination
