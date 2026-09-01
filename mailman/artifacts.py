from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from mailman.models import AgentConfig, RunRecord


_COMMIT_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


def default_data_root() -> Path:
    return Path.cwd() / ".mailman" / "runs"


def new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{secrets.token_hex(3)}"


def create_run(
    *,
    repository: str,
    issue: str,
    base_commit: str,
    primary: str,
    reviewer: str,
    primary_model: str | None = None,
    reviewer_model: str | None = None,
    data_root: Path | None = None,
) -> tuple[RunRecord, Path]:
    if not repository.startswith(("https://", "ssh://", "git@")):
        raise ValueError("repository must be an HTTPS or SSH Git URL")
    if not issue.startswith("https://"):
        raise ValueError("issue must be an HTTPS URL")
    if not _COMMIT_PATTERN.fullmatch(base_commit):
        raise ValueError(
            "base_commit must be a full 40 or 64 character hexadecimal Git object ID"
        )
    if not primary.strip() or not reviewer.strip():
        raise ValueError("primary and reviewer names are required")

    run = RunRecord(
        run_id=new_run_id(),
        repository=repository,
        issue=issue,
        base_commit=base_commit.lower(),
        primary=AgentConfig(agent=primary, model=primary_model),
        reviewer=AgentConfig(agent=reviewer, model=reviewer_model),
    )
    root = (data_root or default_data_root()).resolve()
    run_directory = root / run.run_id
    run_directory.mkdir(parents=True, exist_ok=False)
    (run_directory / "commands").mkdir()
    write_run(run, run_directory)
    (run_directory / "issue.md").write_text(
        f"# Issue\n\nSource: {issue}\n\nIssue content has not been captured.\n",
        encoding="utf-8",
    )
    (run_directory / "primary-report.md").write_text(
        "# Primary report\n\nNot started.\n", encoding="utf-8"
    )
    (run_directory / "review-report.md").write_text(
        "# Review report\n\nNot started.\n", encoding="utf-8"
    )
    (run_directory / "verification.json").write_text("[]\n", encoding="utf-8")
    (run_directory / "toolchain.json").write_text(
        json.dumps({"schema_version": 1, "tools": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    return run, run_directory


def write_run(run: RunRecord, run_directory: Path) -> None:
    destination = run_directory / "run.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(run.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(destination)


def load_run(run_id: str, data_root: Path | None = None) -> tuple[RunRecord, Path]:
    root = (data_root or default_data_root()).resolve()
    run_directory = (root / run_id).resolve()
    if run_directory.parent != root:
        raise ValueError("invalid run ID")
    data = json.loads((run_directory / "run.json").read_text(encoding="utf-8"))
    return RunRecord.from_dict(data), run_directory


def append_verification(run_directory: Path, result: dict[str, object]) -> int:
    path = run_directory / "verification.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    records.append(result)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    command_number = len(records)
    command_path = run_directory / "commands" / f"{command_number:04d}.json"
    command_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return command_number


def append_agent_execution(
    run_directory: Path, role: str, result: dict[str, object]
) -> Path:
    execution_directory = run_directory / "agent-executions"
    execution_directory.mkdir(exist_ok=True)
    sequence = len(list(execution_directory.glob("*.json"))) + 1
    destination = execution_directory / f"{sequence:04d}-{role}.json"
    destination.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return destination
