from __future__ import annotations

from pathlib import Path
from typing import Sequence

from mailman.models import RunRecord
from mailman.prior_art import load_prior_art_markdown


PRIMARY_TASK_FILENAME = "primary-task.md"
REVIEWER_TASK_FILENAME = "reviewer-task.md"


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


def _permitted_command_note(verification_command: Sequence[str] | None) -> str:
    """Say which command is pre-approved, so no turn is spent discovering it.

    An agent that has to guess the permitted spelling learns it only from
    refusals, and a refusal costs a turn out of the same budget the work needs.
    """
    if not verification_command:
        return ""
    printable = " ".join(verification_command)
    return (
        f"""
Run it yourself as `{printable}`. That command is pre-approved, as are the
equivalent spellings of the same interpreter path. A compound command, anything
joined with `&&` or `;`, is refused whatever it contains, so run one command at
a time.
"""
    )


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


def build_primary_prompt(
    run: RunRecord,
    issue_markdown: str,
    *,
    verification_command: Sequence[str] | None,
    prior_art: str | None = None,
) -> str:
    return f"""# Primary engineering task

You are the primary engineer on an unfamiliar repository. The working directory
is the repository root, checked out at exactly `{run.base_commit}`.

Own the issue below through diagnosis, implementation, tests, and a short
evidence report. Read the repository's own contribution and testing
instructions before editing, and follow its existing conventions.

{_verification_line(verification_command)}
{_permitted_command_note(verification_command)}
## Required behavior

- Keep the change focused on this issue. No drive-by refactors.
- Reproduce the reported behavior before changing it when the environment allows.
- Inspect the existing tests before designing regression coverage.
- Do not push, open a pull request, comment on the issue, or otherwise contact
  the upstream repository. Stop at a change in this workspace.
- Report commands you ran and their results, limitations, assumptions, and any
  failure you could not resolve. Separate what you observed from what you infer.

## Issue

{issue_markdown.strip()}
{_prior_art_section(prior_art, audience="primary")}"""


def build_reviewer_prompt(
    run: RunRecord,
    issue_markdown: str,
    *,
    verification_command: Sequence[str] | None,
    prior_art: str | None = None,
) -> str:
    return f"""# Reviewer task

You are a read-only maintainer reviewing a candidate change. Do not edit any
file. The working directory is the repository, and the primary engineer's
uncommitted change sits on top of base commit `{run.base_commit}`.

Read the change with `git diff {run.base_commit}` and read the surrounding code
it touches.

{_verification_line(verification_command)}

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
{_prior_art_section(prior_art, audience="reviewer")}"""


def write_task_prompts(
    run: RunRecord,
    run_directory: Path,
    *,
    verification_command: Sequence[str] | None = None,
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
    primary_path = run_directory / PRIMARY_TASK_FILENAME
    reviewer_path = run_directory / REVIEWER_TASK_FILENAME
    primary_path.write_text(
        build_primary_prompt(
            run,
            issue_markdown,
            verification_command=verification_command,
            prior_art=prior_art,
        ),
        encoding="utf-8",
    )
    reviewer_path.write_text(
        build_reviewer_prompt(
            run,
            issue_markdown,
            verification_command=verification_command,
            prior_art=prior_art,
        ),
        encoding="utf-8",
    )
    return primary_path, reviewer_path
