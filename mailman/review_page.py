"""Render one run as a single self-contained page a person can decide from.

`mailman show` prints a run to a terminal, which is fine for watching one and
useless for judging one. A patch is read, not scrolled past: the diff belongs
next to the gate results, the verdict, and the transcripts that produced them.

The page is one HTML file with no network dependency, because run evidence
lives on a laptop and often on a plane. Everything here came from an agent, an
issue, or a command, so every string is escaped on the way in. See DESIGN.md.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from mailman.transcript import parse_stream
from mailman.view import AgentExecution, agent_executions

_STYLE = """
:root {
  --bg: #f6f7f9; --surface: #ffffff; --border: #d8dee4; --ink: #1c2128;
  --muted: #5b6773; --accent: #0969da; --ok: #1a7f37; --stop: #cf222e;
  --warn: #9a6700; --add-bg: #e6ffec; --del-bg: #ffebe9; --gutter: #f2f4f7;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --surface: #151b23; --border: #2a323c; --ink: #e6edf3;
    --muted: #9198a1; --accent: #4493f8; --ok: #3fb950; --stop: #f85149;
    --warn: #d29922; --add-bg: #12261e; --del-bg: #25171c; --gutter: #11171f;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 400 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
code, pre, .mono { font-family: "Cascadia Mono", Consolas, "SF Mono", Menlo, monospace; }
a { color: var(--accent); }
.page { max-width: 1180px; margin: 0 auto; padding: 24px 16px 48px; }
.masthead { display: flex; flex-wrap: wrap; gap: 12px; align-items: baseline; justify-content: space-between; }
h1 { font: 600 22px/1.3 "Cascadia Mono", Consolas, monospace; margin: 0; }
h2 { font-size: 15px; font-weight: 600; margin: 0 0 12px; }
.sub { color: var(--muted); margin: 8px 0 0; }
.target { margin: 8px 0 0; font-size: 14px; }
.target strong { font-weight: 600; }
.pill {
  display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
  border: 1px solid currentColor;
}
.pill.ok { color: var(--ok); } .pill.stop { color: var(--stop); } .pill.warn { color: var(--warn); }
section { margin-top: 32px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; }
.verdict { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 1px; background: var(--border); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; margin-top: 16px; }
.verdict div { background: var(--surface); padding: 16px; }
.verdict .label, .facts .label { display: block; font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin-bottom: 4px; }
.verdict .value { font-size: 15px; font-weight: 600; }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
.facts div { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px 16px; }
.facts .value { font-family: "Cascadia Mono", Consolas, monospace; font-size: 12.5px; word-break: break-word; }
table.timeline { width: 100%; border-collapse: collapse; font-size: 13px; }
table.timeline td { border-top: 1px solid var(--border); padding: 8px 12px; vertical-align: top; }
table.timeline tr:first-child td { border-top: 0; }
table.timeline .at { color: var(--muted); font-family: "Cascadia Mono", Consolas, monospace; white-space: nowrap; width: 1%; }
table.timeline .mark { width: 1%; white-space: nowrap; font-weight: 600; }
.mark.ok { color: var(--ok); } .mark.stop { color: var(--stop); }
.file { margin-bottom: 16px; overflow: hidden; }
.file > header { display: flex; flex-wrap: wrap; gap: 8px; justify-content: space-between; align-items: center; padding: 10px 16px; border-bottom: 1px solid var(--border); background: var(--gutter); }
.file > header .path { font-family: "Cascadia Mono", Consolas, monospace; font-size: 12.5px; font-weight: 600; word-break: break-all; }
.stat .add { color: var(--ok); font-weight: 600; } .stat .del { color: var(--stop); font-weight: 600; }
.diff { overflow-x: auto; }
.diff table { border-collapse: collapse; width: 100%; font-size: 12.5px; line-height: 1.5; }
.diff td { padding: 0 8px; white-space: pre; vertical-align: top; font-family: "Cascadia Mono", Consolas, monospace; }
.diff td.num { color: var(--muted); text-align: right; width: 1%; user-select: none; background: var(--gutter); border-right: 1px solid var(--border); }
.diff td.sign { width: 1%; color: var(--muted); user-select: none; }
.diff tr.add td:not(.num) { background: var(--add-bg); }
.diff tr.del td:not(.num) { background: var(--del-bg); }
.diff tr.meta td { color: var(--muted); background: var(--gutter); }
pre.block { margin: 0; padding: 12px 16px; overflow-x: auto; font-size: 12.5px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
details.event, details.panel { border-top: 1px solid var(--border); }
details.panel { border: 1px solid var(--border); border-radius: 8px; background: var(--surface); margin-bottom: 12px; }
summary { cursor: pointer; padding: 10px 16px; font-size: 13px; }
summary::-webkit-details-marker { color: var(--muted); }
.events { list-style: none; margin: 0; padding: 0; }
.events li { display: flex; gap: 12px; padding: 6px 16px; border-top: 1px solid var(--border); font-size: 12.5px; font-family: "Cascadia Mono", Consolas, monospace; }
.events li:first-child { border-top: 0; }
.kind { color: var(--muted); width: 72px; flex: none; }
.events .text { word-break: break-word; }
.events li.denied .text { color: var(--stop); }
.note { color: var(--muted); font-size: 13px; margin: 0 0 12px; }
@media (max-width: 700px) { .page { padding: 16px 12px 32px; } h1 { font-size: 18px; } }
"""

_KIND_STYLES = {"error": "denied"}


def _escape(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


@dataclass(frozen=True)
class DiffFile:
    """One file's hunks, already split into displayable rows."""

    path: str
    added: int
    removed: int
    rows: list[tuple[str, str, str, str]]


def parse_diff(text: str) -> list[DiffFile]:
    """Split a unified diff into files and numbered rows.

    Written here rather than shelling out to a diff renderer, because the page
    has to open with no network and the input is a file Mailman itself wrote.
    """
    files: list[DiffFile] = []
    path = ""
    rows: list[tuple[str, str, str, str]] = []
    added = removed = 0
    old_line = new_line = 0

    def flush() -> None:
        nonlocal path, rows, added, removed
        if path:
            files.append(DiffFile(path, added, removed, rows))
        path, rows, added, removed = "", [], 0, 0

    for line in text.splitlines():
        if line.startswith("diff --git "):
            flush()
            parts = line.split(" b/", 1)
            path = parts[1] if len(parts) == 2 else line[len("diff --git ") :]
            continue
        if not path:
            continue
        if line.startswith(("index ", "--- ", "+++ ", "new file", "deleted file", "similarity", "rename ", "old mode", "new mode")):
            continue
        if line.startswith("@@"):
            marker = line.split("@@")
            numbers = marker[1] if len(marker) > 1 else ""
            old_line, new_line = _hunk_start(numbers)
            rows.append(("meta", "", "", line))
            continue
        if line.startswith("+"):
            added += 1
            rows.append(("add", "", str(new_line), line[1:]))
            new_line += 1
        elif line.startswith("-"):
            removed += 1
            rows.append(("del", str(old_line), "", line[1:]))
            old_line += 1
        elif line.startswith("\\"):
            rows.append(("meta", "", "", line))
        else:
            rows.append(("ctx", str(old_line), str(new_line), line[1:] if line else ""))
            old_line += 1
            new_line += 1
    flush()
    return files


def _hunk_start(numbers: str) -> tuple[int, int]:
    old = new = 1
    for part in numbers.split():
        try:
            if part.startswith("-"):
                old = int(part[1:].split(",")[0])
            elif part.startswith("+"):
                new = int(part[1:].split(",")[0])
        except ValueError:
            continue
    return old, new


def _pill(text: str, tone: str) -> str:
    return f'<span class="pill {tone}">{_escape(text)}</span>'


def _tone_for_status(status: str) -> str:
    if status == "READY_FOR_HUMAN_REVIEW":
        return "ok"
    if status in {"BLOCKED", "ABANDONED"}:
        return "stop"
    return "warn"


def _fact(label: str, value: str, *, mono: bool = True) -> str:
    css = "value" if mono else ""
    return (
        f'<div><span class="label">{_escape(label)}</span>'
        f'<span class="{css}">{value}</span></div>'
    )


def _target_line(run: dict[str, Any], issue: dict[str, Any]) -> str:
    """Name the thing being reviewed, before anything about the run itself."""
    reference = issue.get("reference")
    url = ""
    number = ""
    if isinstance(reference, dict):
        url = str(reference.get("url") or "")
        number = str(reference.get("number") or "")
    repository = str(run.get("repository") or "")
    repository = repository.removesuffix(".git").replace("https://github.com/", "")
    title = str(issue.get("title") or "")
    label = f"{repository}#{number}" if repository and number else repository or "target"
    linked = f'<a href="{_escape(url)}">{_escape(label)}</a>' if url else _escape(label)
    state = str(issue.get("state") or "")
    tail = f" &middot; {_escape(state.lower())} upstream" if state else ""
    heading = f"{linked}{tail}"
    if title:
        heading = f"{heading}<br><strong>{_escape(title)}</strong>"
    return f'<p class="target">{heading}</p>'


def _verdict_strip(
    run: dict[str, Any], orchestration: dict[str, Any], steps: list[dict[str, Any]]
) -> str:
    status = str(run.get("status") or orchestration.get("final_status") or "UNKNOWN")
    verdict = "not reached"
    verdict_tone = "warn"
    for step in steps:
        if step.get("name") == "verdict":
            verdict = str((step.get("data") or {}).get("verdict") or "missing")
            verdict_tone = "ok" if verdict == "APPROVE" else "stop"
    gates = [step for step in steps if str(step.get("name", "")).startswith("verification:")]
    passed = sum(1 for gate in gates if gate.get("ok"))
    gate_tone = "ok" if gates and passed == len(gates) else ("stop" if gates else "warn")
    gate_text = f"{passed} of {len(gates)} passed" if gates else "never ran"
    revisions = orchestration.get("revisions_used", 0)
    cycles = orchestration.get("review_cycles", 0)
    return (
        '<div class="verdict">'
        f'<div><span class="label">Run status</span><span class="value">'
        f"{_pill(status, _tone_for_status(status))}</span></div>"
        f'<div><span class="label">Reviewer verdict</span><span class="value">'
        f"{_pill(verdict, verdict_tone)}</span></div>"
        f'<div><span class="label">Mailman verification</span><span class="value">'
        f"{_pill(gate_text, gate_tone)}</span>"
        f'<p class="note" style="margin:8px 0 0">{_escape(revisions)} revision(s), '
        f"{_escape(cycles)} review cycle(s)</p></div>"
        "</div>"
    )


def _facts(
    run: dict[str, Any], issue_record: dict[str, Any], executions: list[AgentExecution]
) -> str:
    issue = run.get("issue")
    if isinstance(issue, dict):
        issue = issue.get("url") or issue.get("reference") or ""
    if not issue:
        reference = issue_record.get("reference")
        issue = reference.get("url", "") if isinstance(reference, dict) else ""
    issue_html = (
        f'<a href="{_escape(issue)}">{_escape(issue)}</a>' if issue else "not recorded"
    )
    cells = [
        _fact("Issue", issue_html),
        _fact("Repository", _escape(run.get("repository", "not recorded"))),
        _fact("Base commit", _escape(run.get("base_commit", "not recorded"))),
    ]
    for role in ("primary", "reviewer"):
        configured = run.get(role) or {}
        execution = next((item for item in executions if item.role == role), None)
        agent = _escape(configured.get("agent", "?"))
        model = configured.get("model") or "vendor default, not recorded"
        detail = f"{agent} &middot; {_escape(model)}"
        if execution is not None:
            budget = execution.record.get("turn_budget")
            seconds = (execution.record.get("process") or {}).get("duration_seconds")
            extras = []
            if isinstance(seconds, (int, float)):
                extras.append(f"{seconds / 60:.1f} min" if seconds >= 90 else f"{seconds:.0f}s")
            if budget:
                extras.append(f"{_escape(budget)} turn budget")
            if extras:
                detail += "<br>" + " &middot; ".join(extras)
        cells.append(_fact(role.capitalize(), detail))
    return f'<div class="facts">{"".join(cells)}</div>'


def _timeline(steps: Iterable[dict[str, Any]]) -> str:
    rows = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        ok = bool(step.get("ok"))
        rows.append(
            f'<tr><td class="at">{_escape(str(step.get("at", ""))[11:19])}</td>'
            f'<td class="mark {"ok" if ok else "stop"}">{"ok" if ok else "stop"}</td>'
            f"<td><strong>{_escape(step.get('name', '?'))}</strong></td>"
            f"<td>{_escape(step.get('detail', ''))}</td></tr>"
        )
    if not rows:
        return '<p class="note">No orchestration record was written for this run.</p>'
    return f'<div class="card"><table class="timeline">{"".join(rows)}</table></div>'


def _diff_summary(diff_text: str) -> str:
    files = parse_diff(diff_text)
    if not files:
        return ""
    added = sum(item.added for item in files)
    removed = sum(item.removed for item in files)
    return (
        f'<p class="note">{len(files)} file(s) changed, '
        f'<span class="stat"><span class="add">+{added}</span> '
        f'<span class="del">-{removed}</span></span>. '
        "Read this before anything below it.</p>"
    )


def _diff_section(diff_text: str) -> str:
    if not diff_text.strip():
        return (
            '<p class="note">No patch was exported for this run. Run '
            "<code>mailman export-patch RUN_ID</code> once it has finished.</p>"
        )
    parts = []
    for changed in parse_diff(diff_text):
        rows = []
        for kind, old, new, content in changed.rows:
            sign = {"add": "+", "del": "-"}.get(kind, "")
            rows.append(
                f'<tr class="{kind}"><td class="num">{_escape(old)}</td>'
                f'<td class="num">{_escape(new)}</td>'
                f'<td class="sign">{sign}</td><td>{_escape(content)}</td></tr>'
            )
        parts.append(
            f'<div class="card file"><header><span class="path">{_escape(changed.path)}</span>'
            f'<span class="stat"><span class="add">+{changed.added}</span> '
            f'<span class="del">-{changed.removed}</span></span></header>'
            f'<div class="diff"><table>{"".join(rows)}</table></div></div>'
        )
    return "".join(parts)


def _verifications(run_directory: Path) -> str:  # noqa: C901
    directory = run_directory / "commands"
    if not directory.is_dir():
        return '<p class="note">No verification command was recorded.</p>'
    blocks = []
    for path in sorted(directory.glob("*.json")):
        record = _read_json(path)
        exit_code = record.get("exit_code")
        timed_out = bool(record.get("timed_out"))
        ok = not timed_out and exit_code == 0
        command = " ".join(str(part) for part in record.get("command", []))
        outcome = "timed out" if timed_out else f"exit {exit_code}"
        tail = "\n".join(
            (str(record.get("stdout", "")) + str(record.get("stderr", "")))
            .strip()
            .splitlines()[-12:]
        )
        blocks.append(
            f'<details class="panel"><summary>{_pill(outcome, "ok" if ok else "stop")} '
            f'<span class="mono">'
            f"{_escape(_shorten(_shorten_paths(command, run_directory)))}</span></summary>"
            f'<pre class="block">'
            f'{_escape(_shorten_paths(tail, run_directory) or "no output")}</pre></details>'
        )
    if not blocks:
        return '<p class="note">No verification command was recorded.</p>'
    return "".join(blocks)


def _shorten_paths(text: str, run_directory: Path) -> str:
    """Trade the host's absolute paths for the run-relative ones.

    Every transcript line on a Windows host carries a 90-character path prefix
    that is the same on every line and tells the reader nothing.
    """
    replaced = str(text)
    for base, token in (
        (str(run_directory.resolve() / "workspace"), "<workspace>"),
        (str(run_directory.resolve()), "<run>"),
        (str(Path.home()), "~"),
    ):
        for variant in (base, base.replace(chr(92), "/")):
            replaced = replaced.replace(variant, token)
    # The tail of a shortened path keeps the host's separators, which now look
    # like escapes rather than directories. Only the tail is touched.
    return _TAIL.sub(lambda match: match.group(0).replace(chr(92), "/"), replaced)


_TAIL = re.compile(r"(?:<workspace>|<run>|~)[\\/][\w./\\+-]*")


def _shorten(text: str, limit: int = 150) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _transcripts(executions: list[AgentExecution], run_directory: Path) -> str:
    blocks = []
    for execution in executions:
        events = parse_stream(execution.stdout.splitlines(), execution.agent)
        items = []
        for event in events:
            css = _KIND_STYLES.get(event.kind, "")
            items.append(
                f'<li class="{css}"><span class="kind">{_escape(event.kind)}</span>'
                f'<span class="text">'
                f"{_escape(_shorten(_shorten_paths(event.summary, run_directory), 220))}"
                "</span></li>"
            )
        body = (
            f'<ul class="events">{"".join(items)}</ul>'
            if items
            else '<p class="note" style="padding:12px 16px">No machine-readable '
            "output was captured for this agent.</p>"
        )
        blocks.append(
            f'<details class="panel"><summary><strong>{_escape(execution.role)}</strong> '
            f"&middot; {_escape(execution.agent)} &middot; {len(events)} event(s)</summary>"
            f"{body}</details>"
        )
    if not blocks:
        return '<p class="note">No agent execution was recorded.</p>'
    return "".join(blocks)


def _reports(run_directory: Path) -> str:
    blocks = []
    for role in ("primary", "reviewer"):
        text = _read_text(run_directory / f"{role}-report.md")
        if not text:
            continue
        blocks.append(
            f'<details class="panel" open><summary><strong>{_escape(role)}</strong> '
            f"report</summary><pre class=\"block\">{_escape(text)}</pre></details>"
        )
    if not blocks:
        return '<p class="note">Neither agent wrote a report.</p>'
    return "".join(blocks)


def render_run_page(run_directory: Path) -> str:
    """Render one run directory as a complete HTML document."""
    run = _read_json(run_directory / "run.json")
    orchestration = _read_json(run_directory / "orchestration.json")
    steps = [step for step in orchestration.get("steps", []) if isinstance(step, dict)]
    executions = agent_executions(run_directory)
    run_id = str(run.get("run_id", run_directory.name))
    issue = _read_json(run_directory / "issue.json")
    diff_text = _read_text(run_directory / "export" / "changes.diff")
    decision = run.get("human_decision") or "none recorded"
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(run_id)} review</title>
<style>{_STYLE}</style></head>
<body><div class="page">
<div class="masthead">
  <div><h1>{_escape(run_id)}</h1>
  {_target_line(run, issue)}
  <p class="sub">Nothing on this page has been sent anywhere. The decision is
  yours: this patch goes upstream, or it does not.</p></div>
</div>
{_verdict_strip(run, orchestration, steps)}
<section><h2>The patch</h2>
{_diff_summary(diff_text)}
{_diff_section(diff_text)}</section>
<section><h2>Run</h2>{_facts(run, issue, executions)}</section>
<section><h2>Verification Mailman ran itself</h2>
<p class="note">Mailman's own gate, not an agent's account of it.</p>
{_verifications(run_directory)}</section>
<section><h2>What the agents reported</h2>{_reports(run_directory)}</section>
<section><h2>What the agents actually did</h2>
<p class="note">Every command and edit, as the agent's own stream reported it.
Paths are shown relative to the run.</p>
{_transcripts(executions, run_directory)}</section>
<section><h2>Timeline</h2>{_timeline(steps)}</section>
<section><h2>Human decision</h2>
<p class="note">Recorded in <code>run.json</code>: {_escape(decision)}</p></section>
</div></body></html>
"""


def write_run_page(run_directory: Path, destination: Path | None = None) -> Path:
    """Write the review page for a run and return where it landed."""
    target = destination or (run_directory / "review.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_run_page(run_directory), encoding="utf-8")
    return target
