"""Decide whether an issue is worth starting a run against.

The duplicate search used to run at submission time, after two agents and a
full environment build had already been spent. That is backwards. An issue
with an open pull request against it is a bad target however good the patch
turns out to be, and that is knowable before anything is cloned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mailman.submission import DUPLICATE_SEARCH_FILENAME

PRIOR_ART_FILENAME = "prior-art.json"

NO_DUPLICATE_SEARCH = "no-duplicate-search"
OPEN_PULL_REQUEST = "open-pull-request"
UNACKNOWLEDGED_ATTEMPTS = "unacknowledged-prior-attempts"


@dataclass(frozen=True)
class TargetAssessment:
    """What the recorded searches say about an issue, before a run starts."""

    searched: bool
    open_attempts: list[dict[str, Any]] = field(default_factory=list)
    closed_attempts: list[dict[str, Any]] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def may_start(self) -> bool:
        return not self.blocking

    def to_dict(self) -> dict[str, Any]:
        return {
            "searched": self.searched,
            "open_attempts": self.open_attempts,
            "closed_attempts": self.closed_attempts,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "may_start": self.may_start,
        }

    def summary(self) -> str:
        lines: list[str] = []
        if not self.searched:
            lines.append(
                "No duplicate search is recorded for this run. Run "
                "`mailman duplicate-search RUN_ID --query ...` first."
            )
        for attempt in self.open_attempts:
            lines.append(
                f"open      #{attempt.get('number')} {attempt.get('title', '')} "
                f"({attempt.get('url', '')})"
            )
        for attempt in self.closed_attempts:
            lines.append(
                f"closed    #{attempt.get('number')} {attempt.get('title', '')} "
                f"({attempt.get('url', '')})"
            )
        if self.open_attempts:
            lines.append(
                "An open pull request means someone is already on this. A "
                "second one is the contribution maintainers close and ban for."
            )
        if self.closed_attempts and not self.open_attempts:
            lines.append(
                "Closed attempts usually mean the maintainers rejected the "
                "approach, not the code. Read them before repeating one. Pass "
                "--acknowledge-prior-attempts to start anyway."
            )
        if not lines:
            lines.append("No prior attempt found. This target looks unclaimed.")
        return "\n".join(lines)


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def assess_target(
    run_directory: Path, *, acknowledged: bool = False
) -> TargetAssessment:
    """Judge a target from the searches already recorded in the run."""
    duplicate_search = _read(run_directory / DUPLICATE_SEARCH_FILENAME)
    prior_art = _read(run_directory / PRIOR_ART_FILENAME)
    searched = duplicate_search.get("success") is True

    attempts = prior_art.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    open_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict) and attempt.get("outcome") == "open"
    ]
    closed_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict) and attempt.get("outcome") != "open"
    ]

    blocking: list[str] = []
    warnings: list[str] = []
    if not searched:
        blocking.append(NO_DUPLICATE_SEARCH)
    if open_attempts:
        # Deliberately not overridable. Every escape hatch here is one someone
        # takes at the wrong moment, and `run-agent` still exists for a
        # deliberate run against a claimed issue.
        blocking.append(OPEN_PULL_REQUEST)
    if closed_attempts:
        if acknowledged:
            warnings.append(UNACKNOWLEDGED_ATTEMPTS)
        else:
            blocking.append(UNACKNOWLEDGED_ATTEMPTS)

    return TargetAssessment(
        searched=searched,
        open_attempts=open_attempts,
        closed_attempts=closed_attempts,
        blocking=blocking,
        warnings=warnings,
    )
