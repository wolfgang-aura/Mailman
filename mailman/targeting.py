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

from mailman.claims import CLAIMS_FILENAME
from mailman.reproduction import REPRODUCTION_FILENAME
from mailman.submission import DUPLICATE_SEARCH_FILENAME
from mailman.target_intel import TARGET_INTEL_FILENAME

PRIOR_ART_FILENAME = "prior-art.json"

NO_DUPLICATE_SEARCH = "no-duplicate-search"
NO_TARGET_INTEL = "no-target-intel"
NO_CLAIM_CHECK = "no-claim-check"
ISSUE_ASSIGNED = "issue-assigned"
WORK_HANDED_OVER = "work-handed-over"
UNACKNOWLEDGED_CLAIM = "unacknowledged-claim"
NO_REPRODUCTION = "no-reproduction"
BUG_NOT_REPRODUCED = "bug-not-reproduced"
UNVERIFIED_REPRODUCTION = "reproduction-not-machine-checked"
FAILS_FRESHNESS_BAR = "fails-freshness-bar"
OPEN_PULL_REQUEST = "open-pull-request"
# The same code `prepare-submission` uses for a merged duplicate. The
# judgement is identical; `check-target` only reaches it earlier.
ALREADY_FIXED_UPSTREAM = "already-fixed-upstream"
UNACKNOWLEDGED_ATTEMPTS = "unacknowledged-prior-attempts"


@dataclass(frozen=True)
class TargetAssessment:
    """What the recorded searches say about an issue, before a run starts."""

    searched: bool
    target_read: bool = False
    intel: dict[str, Any] = field(default_factory=dict)
    reproduction: dict[str, Any] = field(default_factory=dict)
    claims: dict[str, Any] = field(default_factory=dict)
    open_attempts: list[dict[str, Any]] = field(default_factory=list)
    merged_attempts: list[dict[str, Any]] = field(default_factory=list)
    closed_attempts: list[dict[str, Any]] = field(default_factory=list)
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def may_start(self) -> bool:
        return not self.blocking

    def to_dict(self) -> dict[str, Any]:
        return {
            "searched": self.searched,
            "target_read": self.target_read,
            "intel": self.intel,
            "reproduction": self.reproduction,
            "claims": self.claims,
            "open_attempts": self.open_attempts,
            "merged_attempts": self.merged_attempts,
            "closed_attempts": self.closed_attempts,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "may_start": self.may_start,
        }

    def _intel_summary(self) -> str:
        """State the merge path in the two sentences that change a decision."""
        freshness = self.intel.get("freshness", {})
        assessment = self.intel.get("assessment", {})
        parts = [
            f"target    {self.intel.get('repository')}: "
            f"{freshness.get('human_outside_merges', 0)} human outside merge(s) in "
            f"{self.intel.get('window_days')} days, "
            f"{freshness.get('outside_pull_requests_closed_unmerged', 0)} outside "
            "pull request(s) closed unmerged in the same window"
        ]
        read = assessment.get("merge_path_rows_read", 0)
        held = assessment.get("merges_whose_author_held_the_assignment", 0)
        if read and held:
            parts.append(
                f"          {held} of {read} outside merge(s) read here held the "
                "linked issue's assignment first. See target-intel.md for the "
                "threads that won them."
            )
        markers = assessment.get("automated_enforcement") or []
        if markers:
            parts.append(
                "          automated rules in force: " + ", ".join(markers)
            )
        return "\n".join(parts)

    def _reproduction_summary(self) -> list[str]:
        """Say whether the bug is still there, in the words that decide it."""
        if not self.reproduction:
            return [
                "Nothing is recorded about whether the reported bug still "
                "happens at the base commit. Run `mailman reproduce RUN_ID -- "
                "<command>` after `prepare-environment`, or record a human "
                "reading with `--not-machine-reproducible --note ...`."
            ]
        if self.reproduction.get("machine_checked") is not True:
            note = self.reproduction.get("note", "")
            return [
                "reproduce  read by a human, not checked by machine: " + note
            ]
        if self.reproduction.get("reproduced") is True:
            return [
                "reproduce  the reported behaviour still happens at the base "
                "commit"
            ]
        failed = [
            check
            for check in self.reproduction.get("checks", [])
            if isinstance(check, dict) and not check.get("passed")
        ]
        lines = [
            "reproduce  the reported behaviour did NOT happen at the base "
            "commit. The bug may already be fixed, or the reproducer may be "
            "wrong. Either way this is not a target yet."
        ]
        for check in failed:
            lines.append(f"           {check.get('name')}: {check.get('detail')}")
        return lines

    def _claims_summary(self) -> list[str]:
        """Say who has already said they are on this, in their own words."""
        if self.claims.get("success") is not True:
            return [
                "Nothing is recorded about who has already claimed this issue "
                "in its own comments. Run `mailman claims RUN_ID`: a claim made "
                "in a comment is the earliest form of prior art and the "
                "duplicate search cannot see it."
            ]
        assignees = self.claims.get("assignees") or []
        lines: list[str] = []
        if assignees:
            lines.append(
                "assigned  this issue is assigned to " + ", ".join(assignees)
            )
        for row in self.claims.get("claims") or []:
            lines.append(
                f"claimed   {row.get('author')} ({row.get('association')}, "
                f"{row.get('created_at')}): {row.get('quote', '')[:200]}"
            )
        for row in self.claims.get("assignments") or []:
            lines.append(
                f"handed    a maintainer, {row.get('author')}, handed the work "
                f"over: {row.get('quote', '')[:200]}"
            )
        if not lines:
            lines.append(
                f"claims    {self.claims.get('comments_read', 0)} comment(s) "
                "read, none of them a claim"
            )
        return lines

    def summary(self) -> str:
        # The prior-art verdict is stated on its own terms. It used to be
        # inferred from "nothing else was printed", which the target-intel line
        # silently swallowed.
        lines: list[str] = []
        if not self.searched:
            lines.append(
                "No duplicate search is recorded for this run. Run "
                "`mailman duplicate-search RUN_ID --query ...` first."
            )
        if not self.target_read:
            lines.append(
                "Nothing is recorded about how this target hands out work. Run "
                "`mailman target-intel RUN_ID` first: it reads the merges that "
                "actually landed, the threads that preceded them, and the rules "
                "the repository enforces automatically."
            )
        elif self.intel:
            lines.append(self._intel_summary())
        lines.extend(self._reproduction_summary())
        lines.extend(self._claims_summary())
        for attempt in self.open_attempts:
            lines.append(
                f"open      #{attempt.get('number')} {attempt.get('title', '')} "
                f"({attempt.get('url', '')})"
            )
        for attempt in self.merged_attempts:
            lines.append(
                f"merged    #{attempt.get('number')} {attempt.get('title', '')} "
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
        if self.claims.get("assignees"):
            lines.append(
                "An assigned issue belongs to whoever holds it. Pick another."
            )
        elif self.claims.get("assignments"):
            lines.append(
                "A maintainer already handed this work to somebody in the "
                "thread. That is an assignment written in prose rather than in "
                "the assignee field, and it counts the same."
            )
        elif self.claims.get("claims"):
            lines.append(
                "Somebody has said in the comments that they are taking this, "
                "and nobody has answered them. Read the thread, then pass "
                "--acknowledge-claims to start anyway."
            )
        if self.merged_attempts:
            lines.append(
                "A merged pull request on this issue is already upstream. "
                "Nobody rejected it; it is what the repository ships. Read it "
                "before assuming there is anything left to fix, and re-check "
                "the reproduction against a tree that includes it."
            )
        if self.closed_attempts and not self.open_attempts:
            lines.append(
                "Closed attempts usually mean the maintainers rejected the "
                "approach, not the code. Read them before repeating one. Pass "
                "--acknowledge-prior-attempts to start anyway."
            )
        if (
            self.searched
            and not self.open_attempts
            and not self.merged_attempts
            and not self.closed_attempts
            and not (self.claims.get("claims") or self.claims.get("assignments"))
            and not self.claims.get("assignees")
        ):
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
    run_directory: Path,
    *,
    acknowledged: bool = False,
    acknowledged_claims: bool = False,
) -> TargetAssessment:
    """Judge a target from the searches already recorded in the run."""
    duplicate_search = _read(run_directory / DUPLICATE_SEARCH_FILENAME)
    prior_art = _read(run_directory / PRIOR_ART_FILENAME)
    intel = _read(run_directory / TARGET_INTEL_FILENAME)
    reproduction = _read(run_directory / REPRODUCTION_FILENAME)
    claims = _read(run_directory / CLAIMS_FILENAME)
    searched = duplicate_search.get("success") is True
    target_read = intel.get("success") is True

    attempts = prior_art.get("attempts")
    attempts = attempts if isinstance(attempts, list) else []
    open_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict) and attempt.get("outcome") == "open"
    ]
    merged_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict) and attempt.get("outcome") == "merged"
    ]
    # Three buckets, because a row's state changes what it means. `merged` used
    # to land here and be reported as a rejection, which is the opposite of the
    # truth. See https://github.com/wolfgang-aura/Mailman/issues/38.
    closed_attempts = [
        attempt
        for attempt in attempts
        if isinstance(attempt, dict)
        and attempt.get("outcome") not in ("open", "merged")
    ]

    blocking: list[str] = []
    warnings: list[str] = []
    if not searched:
        blocking.append(NO_DUPLICATE_SEARCH)
    if not target_read:
        # Not overridable. Reading how a target merges outside work costs a
        # handful of API calls; skipping it cost a whole session on
        # https://github.com/wolfgang-aura/Mailman/issues/35.
        blocking.append(NO_TARGET_INTEL)
    elif not intel.get("assessment", {}).get("passes_freshness_bar", True):
        warnings.append(FAILS_FRESHNESS_BAR)
    if reproduction.get("success") is not True:
        # An agent run against an already-fixed issue is the most expensive way
        # to learn nothing. See
        # https://github.com/wolfgang-aura/Mailman/issues/37.
        blocking.append(NO_REPRODUCTION)
    elif reproduction.get("machine_checked") is not True:
        warnings.append(UNVERIFIED_REPRODUCTION)
    elif reproduction.get("reproduced") is not True:
        # Not overridable, and deliberately so: the right response to a bug
        # that no longer happens is to abandon the run, not to acknowledge it.
        blocking.append(BUG_NOT_REPRODUCED)
    if claims.get("success") is not True:
        # A comment claim is prior art the duplicate search cannot see. See
        # https://github.com/wolfgang-aura/Mailman/issues/36.
        blocking.append(NO_CLAIM_CHECK)
    elif claims.get("assignees"):
        # Not overridable: somebody holds this issue.
        blocking.append(ISSUE_ASSIGNED)
    elif claims.get("assignments"):
        # A maintainer handing the work over is the assignee field written in
        # prose. Not overridable, for the same reason.
        blocking.append(WORK_HANDED_OVER)
    elif claims.get("claims"):
        # An offer nobody answered is worth a human reading rather than a hard
        # stop, so this is the one claim state a flag can clear. Its own flag:
        # --acknowledge-prior-attempts answers a different question, and one
        # flag for two questions is the defect in
        # https://github.com/wolfgang-aura/Mailman/issues/38.
        if acknowledged_claims:
            warnings.append(UNACKNOWLEDGED_CLAIM)
        else:
            blocking.append(UNACKNOWLEDGED_CLAIM)
    if open_attempts:
        # Deliberately not overridable. Every escape hatch here is one someone
        # takes at the wrong moment, and `run-agent` still exists for a
        # deliberate run against a claimed issue.
        blocking.append(OPEN_PULL_REQUEST)
    if merged_attempts:
        # Not overridable. `--acknowledge-prior-attempts` answers "the
        # maintainers closed an attempt"; it has no answer for "upstream
        # already ships this", and `prepare-submission` blocks the same case
        # under the same code hours later.
        blocking.append(ALREADY_FIXED_UPSTREAM)
    if closed_attempts:
        if acknowledged:
            warnings.append(UNACKNOWLEDGED_ATTEMPTS)
        else:
            blocking.append(UNACKNOWLEDGED_ATTEMPTS)

    return TargetAssessment(
        searched=searched,
        target_read=target_read,
        intel=intel,
        reproduction=reproduction,
        claims=claims,
        open_attempts=open_attempts,
        merged_attempts=merged_attempts,
        closed_attempts=closed_attempts,
        blocking=blocking,
        warnings=warnings,
    )
