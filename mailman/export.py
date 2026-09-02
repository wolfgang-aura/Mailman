from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.issue import load_issue_record
from mailman.models import RunRecord, RunStatus
from mailman.redaction import redact
from mailman.toolchain import resolve_tool


def _git(
    git_executable: str,
    workspace: Path,
    arguments: list[str],
    *,
    timeout_seconds: float,
    detail: str,
) -> str:
    """Read Git output verbatim.

    Evidence records go through `execute`, which redacts credential shapes. A
    patch must never be rewritten on its way out, so the diff is read raw here
    and refused later if it matches a credential pattern.
    """
    try:
        completed = subprocess.run(
            [git_executable, "-C", str(workspace), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"{detail}: command timed out") from error
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip()
        raise ValueError(f"{detail}: {message or 'command failed'}")
    return completed.stdout


def _branch_name(run: RunRecord, issue_record: dict[str, Any] | None) -> str:
    reference = (issue_record or {}).get("reference")
    if isinstance(reference, dict) and isinstance(reference.get("number"), int):
        return f"mailman/issue-{reference['number']}"
    return f"mailman/run-{run.run_id}"


def _issue_title(issue_record: dict[str, Any] | None) -> str | None:
    title = (issue_record or {}).get("title")
    return title if isinstance(title, str) and title.strip() else None


def _read_report(run_directory: Path, name: str) -> str:
    path = run_directory / name
    if not path.is_file():
        return "_No report was recorded._"
    return redact(path.read_text(encoding="utf-8", errors="replace")).strip()


def _verification_rows(run_directory: Path) -> list[dict[str, Any]]:
    path = run_directory / "verification.json"
    if not path.is_file():
        return []
    records = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            continue
        rows.append(
            {
                "record": index,
                "command": " ".join(record.get("command", [])),
                "exit_code": record.get("exit_code"),
                "timed_out": record.get("timed_out"),
                "duration_seconds": record.get("duration_seconds"),
            }
        )
    return rows


def _summary_markdown(
    run: RunRecord,
    *,
    run_directory: Path,
    issue_record: dict[str, Any] | None,
    branch: str,
    changed_files: list[str],
    diffstat: str,
) -> str:
    title = _issue_title(issue_record)
    rows = _verification_rows(run_directory)
    lines = [
        f"# Human review package for {run.run_id}",
        "",
        f"- Repository: {run.repository}",
        f"- Issue: {run.issue}" + (f" ({title})" if title else ""),
        f"- Base commit: `{run.base_commit}`",
        f"- Primary agent: {run.primary.agent}"
        + (f" ({run.primary.model})" if run.primary.model else ""),
        f"- Reviewer agent: {run.reviewer.agent}"
        + (f" ({run.reviewer.model})" if run.reviewer.model else ""),
        f"- Final status: {run.status}",
        f"- Review cycles: {run.review_cycles}",
        f"- Suggested branch: `{branch}`",
        "",
        "No agent contacted the upstream repository. Nothing has been pushed.",
        "",
        "## Changed files",
        "",
    ]
    lines.extend(f"- `{path}`" for path in changed_files)
    lines.extend(["", "```", diffstat.strip(), "```", ""])
    lines.extend(["## Verification Mailman ran itself", ""])
    if rows:
        lines.append("| # | Command | Exit code | Timed out | Seconds |")
        lines.append("| --- | --- | --- | --- | --- |")
        for row in rows:
            lines.append(
                f"| {row['record']} | `{row['command']}` | {row['exit_code']} | "
                f"{row['timed_out']} | {row['duration_seconds']} |"
            )
    else:
        lines.append("_No verification command was recorded._")
    lines.extend(
        [
            "",
            "These results come from Mailman running the command in the workspace,",
            "not from an agent's claim about its own work.",
            "",
            "## Primary report",
            "",
            _read_report(run_directory, "primary-report.md"),
            "",
            "## Reviewer report",
            "",
            _read_report(run_directory, "reviewer-report.md"),
            "",
        ]
    )
    return "\n".join(lines)


_PULL_REQUEST_TEMPLATE = """# Draft pull request text

Nothing here has been sent. A human decides whether any of it is used.

## Suggested branch

`{branch}`

## Suggested title

{heading}

## Suggested body

{closes}

Replace the line above with the change summary from `summary.md`, written in the
repository's own voice. State how the change was tested, using the verification
results Mailman captured rather than an agent's claim. Disclose that an AI agent
drafted the change if the repository's contribution guidelines ask for it. Read
that policy before opening anything.
"""


def _pull_request_markdown(
    run: RunRecord, *, issue_record: dict[str, Any] | None, branch: str
) -> str:
    title = _issue_title(issue_record)
    reference = (issue_record or {}).get("reference")
    number = reference.get("number") if isinstance(reference, dict) else None
    heading = title or f"Address {run.issue}"
    closes = f"Closes #{number}." if isinstance(number, int) else f"Refs {run.issue}."
    return _PULL_REQUEST_TEMPLATE.format(
        branch=branch, heading=heading, closes=closes
    )


def export_patch(
    run: RunRecord,
    run_directory: Path,
    *,
    workspace: Path,
    destination: Path,
    timeout_seconds: float = 120,
    require_ready: bool = True,
) -> dict[str, Any]:
    """Write a reviewable patch package from a finished run's workspace."""
    if require_ready and run.status is not RunStatus.READY_FOR_HUMAN_REVIEW:
        raise ValueError(
            f"run {run.run_id} is {run.status}, not READY_FOR_HUMAN_REVIEW. Pass "
            "--allow-unfinished to export a patch from an unfinished run anyway."
        )
    workspace_path = workspace.resolve(strict=True)
    git_executable = resolve_tool(run_directory, "git")

    # New files stay invisible to `git diff` until the index knows their names,
    # and an export that silently dropped them would understate the change.
    _git(
        git_executable,
        workspace_path,
        ["add", "--intent-to-add", "--all"],
        timeout_seconds=timeout_seconds,
        detail="could not stage new files for the diff",
    )
    diff = _git(
        git_executable,
        workspace_path,
        ["diff", "--no-color", run.base_commit],
        timeout_seconds=timeout_seconds,
        detail="could not read the workspace diff",
    )
    diffstat = _git(
        git_executable,
        workspace_path,
        ["diff", "--no-color", "--stat", run.base_commit],
        timeout_seconds=timeout_seconds,
        detail="could not read the workspace diff stat",
    )
    names = _git(
        git_executable,
        workspace_path,
        ["diff", "--no-color", "--name-only", run.base_commit],
        timeout_seconds=timeout_seconds,
        detail="could not list the changed files",
    )
    changed_files = [line for line in names.splitlines() if line.strip()]
    if not diff.strip():
        raise ValueError(
            f"the workspace has no change against base commit {run.base_commit}"
        )
    if redact(diff) != diff:
        # Refusing beats rewriting. A redacted patch would not apply cleanly and
        # the human would never learn which hunk was altered.
        raise ValueError(
            "the diff matches a credential pattern. Mailman will not export a "
            "patch that may contain a secret, and will not rewrite the diff "
            "either. Inspect the workspace by hand."
        )

    issue_record = load_issue_record(run_directory)
    branch = _branch_name(run, issue_record)
    destination_path = destination.resolve()
    destination_path.mkdir(parents=True, exist_ok=True)
    (destination_path / "changes.diff").write_text(
        diff, encoding="utf-8", newline="\n"
    )
    (destination_path / "summary.md").write_text(
        _summary_markdown(
            run,
            run_directory=run_directory,
            issue_record=issue_record,
            branch=branch,
            changed_files=changed_files,
            diffstat=diffstat,
        ),
        encoding="utf-8",
    )
    (destination_path / "pull-request.md").write_text(
        _pull_request_markdown(run, issue_record=issue_record, branch=branch),
        encoding="utf-8",
    )
    record = {
        "schema_version": 1,
        "run_id": run.run_id,
        "exported_at": datetime.now(UTC).isoformat(),
        "status_at_export": str(run.status),
        "base_commit": run.base_commit,
        "workspace": str(workspace_path),
        "destination": str(destination_path),
        "branch": branch,
        "changed_files": changed_files,
        "diff_characters": len(diff),
        "files": ["changes.diff", "summary.md", "pull-request.md"],
    }
    (destination_path / "export.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    return record
