"""Prove the reported bug is still there, before a run is spent on it.

`check-target` asks who else is working on an issue. `duplicate-search` asks
whether the change already exists. Neither asks the prior question: does the
reported behaviour still happen at the base commit we are about to hand the
agent?

Run 20260903T045152Z-369e77 is why this exists. pytest-dev/pytest#14964 was a
precise, same-day, unclaimed regression report. Every gate passed, the
environment built in four steps, and the bug was already fixed on `main` by a
commit that landed after the 9.1.1 tag. Only a hand-built reproduction caught
it, after the cost had been paid. An agent run against an already-fixed issue
is the most expensive way to learn nothing, and it is the one failure that
leaves a plausible-looking patch behind.

See https://github.com/wolfgang-aura/Mailman/issues/37.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailman.executor import CommandResult
from mailman.workspace import WORKSPACE_DIRECTORY, commit_is_ancestor

REPRODUCTION_FILENAME = "reproduction.json"
REPRODUCTION_SCHEMA_VERSION = 1

#: How the record was produced.
BY_COMMAND = "command"
BY_HUMAN = "human"

#: Marks a row in `verification.json` as a reproduction rather than a
#: verification. A reproduction is expected to fail, so counting one as a
#: verification would read a failing gate as evidence and, worse, would let a
#: reproduction that passed stand in for a verification that never ran.
PURPOSE_KEY = "purpose"
REPRODUCTION_PURPOSE = "reproduction"


def is_reproduction_row(record: dict[str, Any]) -> bool:
    return record.get(PURPOSE_KEY) == REPRODUCTION_PURPOSE


def not_reproductions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the rows that are verifications, in their recorded order."""
    return [record for record in records if not is_reproduction_row(record)]


@dataclass(frozen=True)
class Expectation:
    """What the reported bug looks like when it is still there.

    The default is a command that fails. That covers a reproducer written as a
    test, which is most of them. It does not cover pytest #14964, where the
    fixed and unfixed trees both exit non-zero and differ only in the counts:
    `2 passed, 1 error` against `1 passed, 2 errors`. `required_output` is for
    that case, and it is why this is not a bare exit-code check.
    """

    exit_code: int | None = None
    required_output: tuple[str, ...] = ()
    forbidden_output: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_code": self.exit_code,
            "required_output": list(self.required_output),
            "forbidden_output": list(self.forbidden_output),
        }

    def describe(self) -> str:
        parts = [
            "any non-zero exit"
            if self.exit_code is None
            else f"exit code {self.exit_code}"
        ]
        for text in self.required_output:
            parts.append(f"output containing {text!r}")
        for text in self.forbidden_output:
            parts.append(f"output not containing {text!r}")
        return "; ".join(parts)


@dataclass(frozen=True)
class ReproductionCheck:
    name: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ReproductionOutcome:
    reproduced: bool
    checks: list[ReproductionCheck] = field(default_factory=list)

    def failures(self) -> list[ReproductionCheck]:
        return [check for check in self.checks if not check.passed]


def evaluate(result: CommandResult, expectation: Expectation) -> ReproductionOutcome:
    """Judge one command result against what the bug should look like."""
    checks: list[ReproductionCheck] = []
    combined = f"{result.stdout}\n{result.stderr}"

    if result.timed_out:
        # A timeout says the reproducer hung, not that the bug is still there.
        checks.append(
            ReproductionCheck(
                "completed",
                False,
                f"the command timed out after {result.timeout_seconds:g} seconds, "
                "which is not evidence of the reported behaviour",
            )
        )
        return ReproductionOutcome(reproduced=False, checks=checks)
    checks.append(ReproductionCheck("completed", True, "the command ran to completion"))

    if expectation.exit_code is None:
        checks.append(
            ReproductionCheck(
                "exit-code",
                result.exit_code != 0,
                f"exited {result.exit_code}, expected any non-zero exit",
            )
        )
    else:
        checks.append(
            ReproductionCheck(
                "exit-code",
                result.exit_code == expectation.exit_code,
                f"exited {result.exit_code}, expected {expectation.exit_code}",
            )
        )

    for text in expectation.required_output:
        found = text in combined
        checks.append(
            ReproductionCheck(
                "required-output",
                found,
                f"{text!r} was {'found' if found else 'not found'} in the output",
            )
        )
    for text in expectation.forbidden_output:
        found = text in combined
        checks.append(
            ReproductionCheck(
                "forbidden-output",
                not found,
                f"{text!r} was {'found' if found else 'not found'} in the output",
            )
        )

    return ReproductionOutcome(
        reproduced=all(check.passed for check in checks), checks=checks
    )


def record_command_reproduction(
    run_directory: Path,
    *,
    result: CommandResult,
    expectation: Expectation,
    working_directory: Path,
    command_record: int,
    base_commit: str | None = None,
) -> dict[str, Any]:
    """Write what one reproduction attempt proved, or failed to prove."""
    outcome = evaluate(result, expectation)
    record = {
        "schema_version": REPRODUCTION_SCHEMA_VERSION,
        "recorded_at": datetime.now(UTC).isoformat(),
        "method": BY_COMMAND,
        "success": True,
        "machine_checked": True,
        "reproduced": outcome.reproduced,
        "base_commit": base_commit,
        # `execute` already redacted the command and its output.
        "command": list(result.command),
        "working_directory": str(working_directory),
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "duration_seconds": result.duration_seconds,
        "command_record": command_record,
        "expectation": expectation.to_dict(),
        "checks": [check.to_dict() for check in outcome.checks],
        "note": None,
    }
    _write(run_directory, record)
    return record


def record_human_reproduction(
    run_directory: Path, *, note: str, base_commit: str | None = None
) -> dict[str, Any]:
    """Record that a person read the bug and no machine could check it.

    Some issues have no runnable reproducer. "Not reproducible by machine, read
    by a human, and here is what was read" is a worse record than a failing
    command and a better one than the silence it replaces. It warns rather than
    blocks, and the warning is printed by every later `check-target`.
    """
    if not note.strip():
        raise ValueError(
            "a human reproduction record needs a note saying what was read and "
            "why no command can check it"
        )
    record = {
        "schema_version": REPRODUCTION_SCHEMA_VERSION,
        "recorded_at": datetime.now(UTC).isoformat(),
        "method": BY_HUMAN,
        "success": True,
        "machine_checked": False,
        "reproduced": None,
        "base_commit": base_commit,
        "command": None,
        "working_directory": None,
        "exit_code": None,
        "timed_out": None,
        "duration_seconds": None,
        "command_record": None,
        "expectation": None,
        "checks": [],
        "note": note.strip(),
    }
    _write(run_directory, record)
    return record


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def merge_is_in_base(
    run_directory: Path, attempt: dict[str, Any], reproduction: dict[str, Any]
) -> bool:
    """Say whether a merged attempt is already in the tree the run starts from.

    Two things have to be true, and neither is a judgement call. The merge
    commit must be an ancestor of the base commit, so the accepted fix is
    already in the code the agent would be handed. And the reproduction must
    have failed at that same base commit, so the defect demonstrably survived
    it. Together they answer the question `already-fixed-upstream` asks and
    gets wrong: upstream shipped something to this code, and it was not this.

    Anything missing or unreadable is a no. A refusal that cannot be checked
    stays a refusal. See https://github.com/wolfgang-aura/Mailman/issues/46.
    """
    merge_commit = attempt.get("merge_commit")
    if not isinstance(merge_commit, str) or not merge_commit.strip():
        return False
    if reproduction.get("success") is not True:
        return False
    if reproduction.get("machine_checked") is not True:
        return False
    if reproduction.get("reproduced") is not True:
        return False
    base_commit = reproduction.get("base_commit")
    if not isinstance(base_commit, str) or not base_commit.strip():
        return False
    workspace = run_directory / WORKSPACE_DIRECTORY
    if not (workspace / ".git").exists():
        return False
    recorded_head = _read_json(run_directory / "workspace.json").get("head")
    if recorded_head != base_commit:
        # The reproduction ran somewhere other than this clone's base commit,
        # so an ancestry check against this clone proves nothing about it.
        return False
    try:
        return commit_is_ancestor(workspace, merge_commit.strip())
    except (ValueError, OSError):
        return False


def load_reproduction(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / REPRODUCTION_FILENAME
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _write(run_directory: Path, record: dict[str, Any]) -> Path:
    destination = run_directory / REPRODUCTION_FILENAME
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination
