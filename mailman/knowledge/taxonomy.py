from __future__ import annotations

from enum import StrEnum
from typing import Iterable


class PatternCategory(StrEnum):
    """Where an engineering run succeeded or failed.

    The same categories classify successes and failures so a recurring pattern
    can be counted across runs regardless of which channel found it.
    """

    ISSUE_UNDERSTANDING = "ISSUE_UNDERSTANDING"
    REPOSITORY_ORIENTATION = "REPOSITORY_ORIENTATION"
    REPRODUCTION = "REPRODUCTION"
    ROOT_CAUSE = "ROOT_CAUSE"
    IMPLEMENTATION = "IMPLEMENTATION"
    TEST_DESIGN = "TEST_DESIGN"
    VERIFICATION = "VERIFICATION"
    SCOPE_CONTROL = "SCOPE_CONTROL"
    REPOSITORY_CONVENTIONS = "REPOSITORY_CONVENTIONS"
    REVIEW = "REVIEW"
    TOOL_USAGE = "TOOL_USAGE"
    ENVIRONMENT = "ENVIRONMENT"
    MAINTAINER_ALIGNMENT = "MAINTAINER_ALIGNMENT"


class Outcome(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    MIXED = "MIXED"


class EvidenceChannel(StrEnum):
    """Who or what found an observation."""

    MAINTAINER_FEEDBACK = "MAINTAINER_FEEDBACK"
    POST_MERGE_REGRESSION = "POST_MERGE_REGRESSION"
    AUTOMATED_VERIFICATION = "AUTOMATED_VERIFICATION"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    PRIMARY_AGENT_FAILURE = "PRIMARY_AGENT_FAILURE"
    REVIEWER_FINDING = "REVIEWER_FINDING"
    AGENT_RETROSPECTIVE = "AGENT_RETROSPECTIVE"


CHANNEL_WEIGHTS: dict[EvidenceChannel, int] = {
    # A maintainer correction and a post-merge regression are ground truth from
    # outside the run.
    EvidenceChannel.MAINTAINER_FEEDBACK: 6,
    EvidenceChannel.POST_MERGE_REGRESSION: 6,
    # A command result and a human reading the diff are observations.
    EvidenceChannel.AUTOMATED_VERIFICATION: 5,
    EvidenceChannel.HUMAN_REVIEW: 5,
    # A primary agent that timed out, crashed, or produced no report is a fact
    # about the process, not a claim by it.
    EvidenceChannel.PRIMARY_AGENT_FAILURE: 4,
    # An independent model's finding is an opinion worth more than nothing.
    EvidenceChannel.REVIEWER_FINDING: 2,
    # An agent's account of its own work is the weakest evidence in the system.
    EvidenceChannel.AGENT_RETROSPECTIVE: 1,
}

# Evidence at or above this weight was observed by a machine or a human rather
# than asserted by a model.
OBSERVED_EVIDENCE_WEIGHT = 4


class Scope(StrEnum):
    """How far an observation could generalize beyond the run that found it."""

    RUN_ONLY = "RUN_ONLY"
    REPOSITORY = "REPOSITORY"
    ECOSYSTEM = "ECOSYSTEM"
    UNIVERSAL = "UNIVERSAL"


class KnowledgeLayer(StrEnum):
    """Whether guidance applies to every run or only under stated conditions."""

    CORE = "CORE"
    CONDITIONAL = "CONDITIONAL"


def channel_weight(channel: EvidenceChannel) -> int:
    return CHANNEL_WEIGHTS[channel]


def is_observed(channel: EvidenceChannel) -> bool:
    """Report whether a channel carries observation rather than self-report."""
    return channel_weight(channel) >= OBSERVED_EVIDENCE_WEIGHT


def strongest_channel(
    channels: Iterable[EvidenceChannel],
) -> EvidenceChannel | None:
    """Return the highest weighted channel in an iterable, or None when empty."""
    best: EvidenceChannel | None = None
    for channel in channels:
        if best is None or channel_weight(channel) > channel_weight(best):
            best = channel
    return best
