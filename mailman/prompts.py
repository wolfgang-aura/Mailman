from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path

from mailman.maintainer_review import load_review_markdown
from mailman.models import RunRecord
from mailman.prior_art import load_prior_art_markdown

PRIMARY_TASK_FILENAME = "primary-task.md"
REVIEWER_TASK_FILENAME = "reviewer-task.md"
PROMPTS_RECORD_FILENAME = "prompts.json"
PROMPTS_RECORD_SCHEMA_VERSION = 2
WORK_ORDER_FILENAME = "work-order.json"

_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+(?:[\\/][A-Za-z0-9_.-]+)+(?::\d+)?)"
)

# Phrases an issue author uses to say how something other than the target
# handles the same case. python/mypy#21960 named three checkers this way, each
# with its own framing of the rule, and the agents never saw the sentence.
# https://github.com/wolfgang-aura/Mailman/issues/85
_COMPARISON_CUES = re.compile(
    r"\b("
    r"other (?:type )?(?:checkers?|tools?|linters?|implementations?|libraries|"
    r"languages?|runtimes?|compilers?|engines?)"
    r"|unlike|whereas|in contrast|by contrast|compared (?:to|with)|for comparison"
    r"|(?:the|per|according to the) (?:typing |language |json |html |http |css )?"
    r"(?:spec|specification|standard|rfc|pep)\b"
    r")",
    re.IGNORECASE,
)
_MAX_COMPARISONS = 6
_MAX_COMPARISON_CHARS = 700


_EXECUTION_DISCIPLINE = """
## Time and context discipline

This run is optimized for a small upstream patch. Keep shell output narrow:

- Do not print whole large files or an unrestricted repository-wide search.
- Cap searches and file slices to the lines needed for the current decision.
- Read each instruction file once per session. Do not reopen it unless it changed.
- Prefer the recorded focused verification over a full suite.
- Stop and report that the issue is too broad if the fix grows past 8 files or
  about 500 changed lines. Do not turn one issue into a subsystem redesign.
"""


def load_recorded_verification(run_directory: Path) -> list[str] | None:
    """The verification command the built prompts quote, when they quote one.

    `build-prompts` takes free text and turns it into prose for the agents;
    `orchestrate` takes an argv list and runs it as the gate. Without a record
    of the first, a run can carry two unlinked claims about what its
    verification is, written in two formats, and nothing notices when they
    disagree. See https://github.com/wolfgang-aura/Mailman/issues/58.
    """
    path = run_directory / PROMPTS_RECORD_FILENAME
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    command = data.get("verification_command")
    if not isinstance(command, list) or not all(
        isinstance(part, str) for part in command
    ):
        return None
    return command


def _verification_line(verification_command: Sequence[str] | None) -> str:
    if not verification_command:
        return (
            "Mailman runs an independent verification command in this workspace "
            "after you stop. Your process exit code proves nothing on its own."
        )
    printable = " ".join(verification_command)
    return (
        f"Mailman runs `{printable}` in this workspace itself after you stop, and "
        "again after the review. Your process exit code proves nothing on its own."
    )


def _focused_check_note(verification_command: Sequence[str] | None) -> str:
    """Keep the agent's checks focused; the harness owns the full gate."""
    if not verification_command:
        return ""
    printable = " ".join(verification_command)
    return (
        f"""
The full gate is `{printable}`. Mailman runs it after your stage, so do not run
it merely to duplicate the harness. Run the smallest focused test that guides
your work. If the harness gate later fails, you will receive its exact failure
in the same session.
"""
    )


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _known_scope_section(run_directory: Path) -> str:
    prescreen = _read_json(run_directory / "prescreen.json")
    symbols = [str(item) for item in prescreen.get("symbols") or [] if str(item)]
    if not symbols:
        return ""
    return (
        "\n## Pre-screened scope\n\n"
        "Start with these issue symbols; do not begin with a repository-wide "
        f"search: {', '.join(f'`{item}`' for item in symbols)}.\n"
    )


def issue_comparisons(issue_markdown: str) -> list[str]:
    """Paragraphs of the issue body that say how another tool handles the case.

    Prose only: fenced code and the capture header are skipped, and the answer
    is capped so a long issue cannot flood the prompt. A miss costs a sentence
    the agent could have read anyway; a hit tells it which framings the
    maintainers have already been shown.
    """
    body = issue_markdown.split("## Issue body", 1)[-1].split("## Capture boundary", 1)[0]
    found: list[str] = []
    in_fence = False
    paragraph: list[str] = []

    def flush() -> None:
        text = " ".join(line.strip() for line in paragraph).strip()
        paragraph.clear()
        if not text or text.startswith("#") or not _COMPARISON_CUES.search(text):
            return
        if len(text) > _MAX_COMPARISON_CHARS:
            text = text[: _MAX_COMPARISON_CHARS - 3].rstrip() + "..."
        if text not in found:
            found.append(text)

    for line in body.splitlines():
        if line.strip().startswith(("```", "~~~")):
            in_fence = not in_fence
            flush()
            continue
        if in_fence:
            continue
        if not line.strip():
            flush()
            continue
        paragraph.append(line)
    flush()
    return found[:_MAX_COMPARISONS]


def _comparison_section(comparisons: Sequence[str], *, audience: str) -> str:
    if not comparisons:
        return ""
    quoted = "\n".join(f"> {text}" for text in comparisons)
    if audience == "reviewer":
        ask = (
            "Say which of these framings the candidate follows, and whether the "
            "others were considered and why they were not taken. A patch that "
            "picks the narrowest reading when the issue names a broader rule "
            "another implementation enforces is a required change, not a note."
        )
    else:
        ask = (
            "These are framings the maintainers have already been shown. Decide "
            "which rule your change enforces, say so in the report, and say why "
            "the others were not the right mechanism here. Do not pick one "
            "silently."
        )
    return f"""
## How the issue says other implementations handle this

{quoted}

{ask}
"""


def _work_order(
    run_directory: Path,
    issue_markdown: str,
    verification_command: Sequence[str] | None,
    start_files: Sequence[str] = (),
) -> tuple[dict[str, object], str]:
    """Resolve issue hints to files that exist before an agent is started."""
    workspace = run_directory / "workspace"
    reproduction = _read_json(run_directory / "reproduction.json")
    prescreen = _read_json(run_directory / "prescreen.json")
    evidence = "\n".join(
        (
            issue_markdown,
            json.dumps(reproduction.get("command") or []),
            json.dumps(reproduction.get("artifacts") or []),
            json.dumps(prescreen.get("symbols") or []),
        )
    )
    found: set[str] = set()
    if workspace.is_dir():
        root = workspace.resolve()
        candidates = [match.group(1) for match in _PATH_PATTERN.finditer(evidence)]
        candidates.extend(start_files)
        for raw_candidate in candidates:
            candidate = str(raw_candidate).replace("\\", "/")
            candidate = re.sub(r":\d+$", "", candidate)
            if "://" in candidate or candidate.startswith(("github.com/", "www.")):
                continue
            resolved = (workspace / candidate).resolve()
            if resolved.is_relative_to(root) and resolved.is_file():
                found.add(resolved.relative_to(root).as_posix())
    order: dict[str, object] = {
        "schema_version": 1,
        "start_files": sorted(found),
        "symbols": [str(item) for item in prescreen.get("symbols") or [] if str(item)],
        "verification_command": list(verification_command or []),
        "comparisons": issue_comparisons(issue_markdown),
    }
    (run_directory / WORK_ORDER_FILENAME).write_text(
        json.dumps(order, indent=2) + "\n", encoding="utf-8"
    )
    files = order["start_files"]
    if not files:
        return order, ""
    listed = "\n".join(f"- `{path}`" for path in files)
    section = f"""
## Ready-to-code work order

Mailman verified these paths in the prepared workspace. Open them first. Do not
search the repository until one of these files proves that another path is needed.

{listed}

The independent gate is already fixed. Your task is to make the smallest change
that satisfies the reported behavior and that gate.
"""
    return order, section


def _reproduction_section(run_directory: Path) -> str:
    reproduction = _read_json(run_directory / "reproduction.json")
    if reproduction.get("success") is not True:
        return ""
    command = " ".join(str(item) for item in reproduction.get("command") or [])
    if reproduction.get("reproduced") is True:
        outcome = (
            "timed out"
            if reproduction.get("timed_out")
            else f"exited {reproduction.get('exit_code')}"
        )
        artifact_text = _reproduction_artifact_text(run_directory, reproduction)
        return (
            "\n## Baseline already proved by Mailman\n\n"
            f"At the recorded base commit, `{command}` {outcome} and satisfied "
            "the reproduction contract. The exact reproducer source is below when "
            "the command named a workspace file. Before running any command, compare "
            "it with the issue's reported conditions and the repository's supported "
            "setup. If it checks different conditions or behavior, stop "
            "and put `MAILMAN-REPRODUCTION-MISMATCH: REASON` on its own line in your "
            "report. Otherwise, use it as the observed before-state. Do not spend "
            f"time recreating or rerunning the baseline.\n{artifact_text}"
        )
    return ""


def _reproduction_artifact_text(
    run_directory: Path, reproduction: dict
) -> str:
    sections: list[str] = []
    root = run_directory.resolve()
    for artifact in reproduction.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        snapshot_name = artifact.get("snapshot")
        source_name = artifact.get("source")
        if not isinstance(snapshot_name, str) or not isinstance(source_name, str):
            continue
        snapshot = (run_directory / snapshot_name).resolve()
        if not snapshot.is_relative_to(root) or not snapshot.is_file():
            continue
        source = snapshot.read_text(encoding="utf-8", errors="replace")
        sections.append(
            f"\n### Reproducer source: `{source_name}`\n\n```text\n{source}\n```\n"
        )
    return "".join(sections)


def _prior_art_section(prior_art: str | None, *, audience: str) -> str:
    """Put earlier attempts in front of the agent, with what they are for.

    Three pull requests were closed on pytest #14324 before Mailman ever ran on
    it, and both agents re-derived a rejected approach because nothing told them
    those attempts existed.
    """
    if not prior_art:
        return ""
    if audience == "primary":
        instruction = (
            "Read this before you design a fix. An approach that was already "
            "rejected will be rejected again. If your fix resembles one below, "
            "say in your report why yours is different."
        )
    else:
        instruction = (
            "Judge the candidate against these. A candidate that repeats a "
            "rejected approach is not ready, however well it is written."
        )
    return f"""
## Earlier attempts at this issue

{instruction}

{prior_art.strip()}
"""


def _maintainer_review_section(review: str | None, *, audience: str) -> str:
    """Put the maintainer's own words in front of both agents.

    A revision that answers a reviewer without reading them is the most
    expensive kind of work: it costs a second review round and some of the
    maintainer's patience. https://github.com/wolfgang-aura/Mailman/issues/60
    """
    if not review:
        return ""
    if audience == "primary":
        instruction = (
            "This pull request is already filed and a maintainer has asked for "
            "changes. Answer every point below. Where you disagree, say so in "
            "your report and explain why rather than ignoring it. A constraint "
            "the maintainer named is not yours to negotiate away."
        )
    else:
        instruction = (
            "Judge the revision against these requests, not only against the "
            "issue. A revision that leaves one of them unanswered is not "
            "ready, however good the code is."
        )
    return f"""
## The maintainer's review of the filed pull request

{instruction}

{review.strip()}
"""


def build_primary_prompt(
    run: RunRecord,
    issue_markdown: str,
    *,
    verification_command: Sequence[str] | None,
    prior_art: str | None = None,
    maintainer_review: str | None = None,
    scope: str = "",
    reproduction: str = "",
    work_order: str = "",
    comparisons: str = "",
) -> str:
    return f"""# Primary engineering task

You are the primary engineer on an unfamiliar repository. The working directory
is the repository root, checked out at exactly `{run.base_commit}`.

Own the issue below through diagnosis, implementation, tests, and a short
evidence report. Read the repository's own contribution and testing
instructions before editing, and follow its existing conventions.

{_verification_line(verification_command)}
{_focused_check_note(verification_command)}
{_EXECUTION_DISCIPLINE}
{work_order}{scope}{reproduction}{comparisons}
## Required behavior

- Keep the change focused on this issue. No drive-by refactors.
- Use the recorded baseline. Do not repeat coordinator discovery or reproduction.
- Inspect the existing tests before designing regression coverage.
- Do not push, open a pull request, comment on the issue, or otherwise contact
  the upstream repository. Stop at a change in this workspace.
- Report commands you ran and their results, limitations, assumptions, and any
  failure you could not resolve. Separate what you observed from what you infer.
- Once the patch, focused check, and report are complete, stop. Do not spend more
  commands looking for optional improvements.

## Issue

{issue_markdown.strip()}
{_prior_art_section(prior_art, audience="primary")}{_maintainer_review_section(maintainer_review, audience="primary")}"""


def build_reviewer_prompt(
    run: RunRecord,
    issue_markdown: str,
    *,
    verification_command: Sequence[str] | None,
    prior_art: str | None = None,
    maintainer_review: str | None = None,
    scope: str = "",
    reproduction: str = "",
    work_order: str = "",
    comparisons: str = "",
) -> str:
    return f"""# Reviewer task

You are a read-only maintainer reviewing a candidate change. Do not edit any
file. The working directory is the repository, and the primary engineer's
uncommitted change sits on top of base commit `{run.base_commit}`.

Mailman appends the candidate diff and the primary report when review starts.
Read that supplied diff first. Do not use the shell unless a specific unresolved
question requires a bounded slice of surrounding code or one focused check.

{_verification_line(verification_command)}
{_EXECUTION_DISCIPLINE}
{work_order}{scope}{reproduction}{comparisons}
## Judge

- Does the change address the issue below, and only that issue?
- Is the cause fixed, or only the symptom?
- Which edge cases and failure modes are unhandled?
- Is there regression coverage, and does it follow repository conventions?
- Any security, reliability, or compatibility risk?

Do not claim a check passed without a result you captured yourself in this
workspace. List every required change as a short bullet above your verdict.

## Issue

{issue_markdown.strip()}
{_prior_art_section(prior_art, audience="reviewer")}{_maintainer_review_section(maintainer_review, audience="reviewer")}"""


def write_task_prompts(
    run: RunRecord,
    run_directory: Path,
    *,
    verification_command: Sequence[str] | None = None,
    start_files: Sequence[str] = (),
) -> tuple[Path, Path]:
    """Turn the captured issue into a primary and a reviewer prompt."""
    issue_path = run_directory / "issue.md"
    if not issue_path.is_file():
        raise ValueError(
            "no captured issue. Run `mailman fetch-issue` before building prompts."
        )
    issue_markdown = issue_path.read_text(encoding="utf-8")
    if "Issue content has not been captured" in issue_markdown:
        raise ValueError(
            "the issue placeholder is still in place. Run `mailman fetch-issue` "
            "before building prompts."
        )
    prior_art = load_prior_art_markdown(run_directory)
    maintainer_review = load_review_markdown(run_directory)
    scope = _known_scope_section(run_directory)
    reproduction = _reproduction_section(run_directory)
    work_order, work_order_section = _work_order(
        run_directory, issue_markdown, verification_command, start_files
    )
    workspace = run_directory / "workspace"
    if (workspace / ".git").exists() and not work_order["start_files"]:
        raise ValueError(
            "no exact start file could be verified in the prepared workspace. "
            "Name one with `mailman build-prompts --start-file PATH`; do not "
            "send an agent a symbol-only repository search."
        )
    primary_path = run_directory / PRIMARY_TASK_FILENAME
    reviewer_path = run_directory / REVIEWER_TASK_FILENAME
    primary_path.write_text(
        build_primary_prompt(
            run,
            issue_markdown,
            verification_command=verification_command,
            prior_art=prior_art,
            maintainer_review=maintainer_review,
            scope=scope,
            reproduction=reproduction,
            work_order=work_order_section,
            comparisons=_comparison_section(
                work_order["comparisons"], audience="primary"
            ),
        ),
        encoding="utf-8",
    )
    reviewer_path.write_text(
        build_reviewer_prompt(
            run,
            issue_markdown,
            verification_command=verification_command,
            prior_art=prior_art,
            maintainer_review=maintainer_review,
            scope=scope,
            reproduction=reproduction,
            work_order=work_order_section,
            comparisons=_comparison_section(
                work_order["comparisons"], audience="reviewer"
            ),
        ),
        encoding="utf-8",
    )
    record = {
        "schema_version": PROMPTS_RECORD_SCHEMA_VERSION,
        "verification_command": (
            list(verification_command) if verification_command else None
        ),
        "work_order": work_order,
    }
    (run_directory / PROMPTS_RECORD_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    return primary_path, reviewer_path
