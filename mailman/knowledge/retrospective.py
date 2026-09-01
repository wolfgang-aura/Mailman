from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from mailman.knowledge.taxonomy import (
    EvidenceChannel,
    Outcome,
    PatternCategory,
    Scope,
    channel_weight,
    strongest_channel,
)
from mailman.models import utc_now


RETROSPECTIVE_SCHEMA_VERSION = 1

# A version that was asked for and could not be determined. Recorded rather
# than omitted so a retrospective never implies a version it does not have.
UNRECORDED = "unrecorded"

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class Observation:
    """One thing a run taught, and who or what found it."""

    observation_id: str
    channel: EvidenceChannel
    category: PatternCategory
    outcome: Outcome
    summary: str
    assumption: str | None = None
    detail: str = ""
    scope: Scope = Scope.RUN_ONLY
    evidence: tuple[str, ...] = ()
    candidate_guidance: str | None = None

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.observation_id):
            raise ValueError(
                "observation_id must start with a letter or digit and use only "
                "letters, digits, underscores, hyphens, or periods"
            )
        if not self.summary.strip():
            raise ValueError("observation summary is required")

    @property
    def weight(self) -> int:
        return channel_weight(self.channel)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "channel": str(self.channel),
            "category": str(self.category),
            "outcome": str(self.outcome),
            "summary": self.summary,
            "assumption": self.assumption,
            "detail": self.detail,
            "scope": str(self.scope),
            "evidence": list(self.evidence),
            "candidate_guidance": self.candidate_guidance,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Observation:
        return cls(
            observation_id=data["observation_id"],
            channel=EvidenceChannel(data["channel"]),
            category=PatternCategory(data["category"]),
            outcome=Outcome(data["outcome"]),
            summary=data["summary"],
            assumption=data.get("assumption"),
            detail=data.get("detail", ""),
            scope=Scope(data.get("scope", Scope.RUN_ONLY)),
            evidence=tuple(data.get("evidence", ())),
            candidate_guidance=data.get("candidate_guidance"),
        )


@dataclass(frozen=True)
class RunVersions:
    """Everything a later comparison needs to know about what produced a run."""

    skill_version: str = UNRECORDED
    primary_prompt_version: str = UNRECORDED
    review_prompt_version: str = UNRECORDED
    orchestrator_version: str = UNRECORDED
    primary_model: str | None = None
    reviewer_model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_version": self.skill_version,
            "primary_prompt_version": self.primary_prompt_version,
            "review_prompt_version": self.review_prompt_version,
            "orchestrator_version": self.orchestrator_version,
            "primary_model": self.primary_model,
            "reviewer_model": self.reviewer_model,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunVersions:
        return cls(
            skill_version=data.get("skill_version", UNRECORDED),
            primary_prompt_version=data.get("primary_prompt_version", UNRECORDED),
            review_prompt_version=data.get("review_prompt_version", UNRECORDED),
            orchestrator_version=data.get("orchestrator_version", UNRECORDED),
            primary_model=data.get("primary_model"),
            reviewer_model=data.get("reviewer_model"),
        )


@dataclass(frozen=True)
class TemplateSection:
    """A question Mailman cannot answer for itself."""

    key: str
    title: str
    question: str
    channel: EvidenceChannel
    category: PatternCategory
    outcome: Outcome


RETROSPECTIVE_SECTIONS: tuple[TemplateSection, ...] = (
    TemplateSection(
        key="successes",
        title="What went unusually well",
        question=(
            "What worked better than expected, and what specifically made it "
            "work? Name the behavior, not the result."
        ),
        channel=EvidenceChannel.AGENT_RETROSPECTIVE,
        category=PatternCategory.IMPLEMENTATION,
        outcome=Outcome.SUCCESS,
    ),
    TemplateSection(
        key="failures",
        title="What went wrong",
        question="What did not work, and at which stage did it first go wrong?",
        channel=EvidenceChannel.AGENT_RETROSPECTIVE,
        category=PatternCategory.IMPLEMENTATION,
        outcome=Outcome.FAILURE,
    ),
    TemplateSection(
        key="incorrect_assumptions",
        title="Assumptions that decided the outcome",
        question=(
            "Which belief about the issue, repository, or tooling caused the "
            "success or the failure? State the belief, then what was true."
        ),
        channel=EvidenceChannel.AGENT_RETROSPECTIVE,
        category=PatternCategory.ISSUE_UNDERSTANDING,
        outcome=Outcome.MIXED,
    ),
    TemplateSection(
        key="reviewer_findings",
        title="What the reviewer caught",
        question=(
            "Which findings did the reviewing model raise, and was each one "
            "correct? Record rebutted findings too."
        ),
        channel=EvidenceChannel.REVIEWER_FINDING,
        category=PatternCategory.REVIEW,
        outcome=Outcome.MIXED,
    ),
    TemplateSection(
        key="verification_failures",
        title="What automated verification caught",
        question=(
            "Which checks failed, and would the agent have noticed without "
            "them?"
        ),
        channel=EvidenceChannel.AUTOMATED_VERIFICATION,
        category=PatternCategory.VERIFICATION,
        outcome=Outcome.FAILURE,
    ),
    TemplateSection(
        key="human_corrections",
        title="What the human reviewer corrected",
        question="What did a person have to fix, explain, or reject?",
        channel=EvidenceChannel.HUMAN_REVIEW,
        category=PatternCategory.REVIEW,
        outcome=Outcome.FAILURE,
    ),
    TemplateSection(
        key="maintainer_feedback",
        title="What the maintainer said",
        question=(
            "What did the upstream maintainer ask for, reject, or correct? "
            "Leave empty when nothing reached a maintainer."
        ),
        channel=EvidenceChannel.MAINTAINER_FEEDBACK,
        category=PatternCategory.MAINTAINER_ALIGNMENT,
        outcome=Outcome.MIXED,
    ),
    TemplateSection(
        key="unnecessary_work",
        title="Unnecessary work",
        question=(
            "What was changed, read, or run that the issue did not require?"
        ),
        channel=EvidenceChannel.AGENT_RETROSPECTIVE,
        category=PatternCategory.SCOPE_CONTROL,
        outcome=Outcome.FAILURE,
    ),
    TemplateSection(
        key="missing_investigation",
        title="Missing investigation",
        question=(
            "What should have been read, reproduced, or measured before the "
            "first edit?"
        ),
        channel=EvidenceChannel.AGENT_RETROSPECTIVE,
        category=PatternCategory.ROOT_CAUSE,
        outcome=Outcome.FAILURE,
    ),
    TemplateSection(
        key="missing_tests",
        title="Missing tests",
        question=(
            "Which behavior changed without a test that would catch its "
            "regression?"
        ),
        channel=EvidenceChannel.AGENT_RETROSPECTIVE,
        category=PatternCategory.TEST_DESIGN,
        outcome=Outcome.FAILURE,
    ),
    TemplateSection(
        key="tooling_and_environment",
        title="Tooling and environment problems",
        question=(
            "Which tool, sandbox, or environment limit changed what was "
            "possible, and how was it worked around?"
        ),
        channel=EvidenceChannel.AGENT_RETROSPECTIVE,
        category=PatternCategory.ENVIRONMENT,
        outcome=Outcome.FAILURE,
    ),
    TemplateSection(
        key="reusable_lessons",
        title="Possible reusable lessons",
        question=(
            "Which of the above would change how an agent behaves in a "
            "different repository? For each, give the guidance as an "
            "instruction and say what it would have changed here."
        ),
        channel=EvidenceChannel.AGENT_RETROSPECTIVE,
        category=PatternCategory.REPOSITORY_ORIENTATION,
        outcome=Outcome.MIXED,
    ),
)


@dataclass
class Retrospective:
    """What one run taught, seeded by Mailman and completed by a person."""

    run_id: str
    repository: str
    issue: str
    base_commit: str
    run_status: str
    versions: RunVersions = field(default_factory=RunVersions)
    schema_version: int = RETROSPECTIVE_SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now)
    run_facts: dict[str, Any] = field(default_factory=dict)
    observations: list[Observation] = field(default_factory=list)

    def next_observation_id(self) -> str:
        return f"OBS-{len(self.observations) + 1:03d}"

    def add_observation(
        self,
        *,
        channel: EvidenceChannel,
        category: PatternCategory,
        outcome: Outcome,
        summary: str,
        assumption: str | None = None,
        detail: str = "",
        scope: Scope = Scope.RUN_ONLY,
        evidence: tuple[str, ...] = (),
        candidate_guidance: str | None = None,
    ) -> Observation:
        observation = Observation(
            observation_id=self.next_observation_id(),
            channel=channel,
            category=category,
            outcome=outcome,
            summary=summary,
            assumption=assumption,
            detail=detail,
            scope=scope,
            evidence=evidence,
            candidate_guidance=candidate_guidance,
        )
        self.observations.append(observation)
        return observation

    def strongest_evidence(self) -> EvidenceChannel | None:
        return strongest_channel(
            observation.channel for observation in self.observations
        )

    def channel_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for observation in self.observations:
            key = str(observation.channel)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def category_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for observation in self.observations:
            key = str(observation.category)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "repository": self.repository,
            "issue": self.issue,
            "base_commit": self.base_commit,
            "run_status": self.run_status,
            "created_at": self.created_at,
            "versions": self.versions.to_dict(),
            "run_facts": self.run_facts,
            "observations": [
                observation.to_dict() for observation in self.observations
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Retrospective:
        return cls(
            run_id=data["run_id"],
            repository=data["repository"],
            issue=data["issue"],
            base_commit=data["base_commit"],
            run_status=data["run_status"],
            versions=RunVersions.from_dict(data.get("versions", {})),
            schema_version=data.get("schema_version", RETROSPECTIVE_SCHEMA_VERSION),
            created_at=data["created_at"],
            run_facts=data.get("run_facts", {}),
            observations=[
                Observation.from_dict(entry)
                for entry in data.get("observations", [])
            ],
        )


def _facts_table(facts: dict[str, Any]) -> list[str]:
    if not facts:
        return ["Mailman recorded no additional facts for this run.", ""]
    lines = ["| Fact | Value |", "| --- | --- |"]
    for key in sorted(facts):
        value = facts[key]
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value) or "none"
        lines.append(f"| {key} | {value} |")
    lines.append("")
    return lines


def _observation_block(observation: Observation) -> list[str]:
    lines = [
        f"### {observation.observation_id}. {observation.summary}",
        "",
        f"- Channel: `{observation.channel}` (weight {observation.weight})",
        f"- Category: `{observation.category}`",
        f"- Outcome: `{observation.outcome}`",
        f"- Scope: `{observation.scope}`",
    ]
    if observation.assumption:
        lines.append(f"- Assumption: {observation.assumption}")
    if observation.evidence:
        joined = ", ".join(f"`{item}`" for item in observation.evidence)
        lines.append(f"- Evidence: {joined}")
    if observation.candidate_guidance:
        lines.append(f"- Candidate guidance: {observation.candidate_guidance}")
    if observation.detail:
        lines.extend(["", observation.detail])
    lines.append("")
    return lines


def render_retrospective(retrospective: Retrospective) -> str:
    """Render the working surface a human or agent completes by hand."""
    versions = retrospective.versions
    lines = [
        f"# Retrospective for run {retrospective.run_id}",
        "",
        f"Drafted by Mailman at {retrospective.created_at}. The seeded",
        "observations below come from recorded evidence. Everything under",
        "\"Questions to answer\" is empty on purpose: Mailman does not have an",
        "opinion about why a run went the way it did.",
        "",
        "This file is private. Move a lesson into `knowledge/lessons.json` only",
        "after removing anything that should not be public.",
        "",
        "## Run",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Run | `{retrospective.run_id}` |",
        f"| Repository | {retrospective.repository} |",
        f"| Issue | {retrospective.issue} |",
        f"| Base commit | `{retrospective.base_commit}` |",
        f"| Final status | `{retrospective.run_status}` |",
        "",
        "## Versions",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Skill | `{versions.skill_version}` |",
        f"| Primary prompt | `{versions.primary_prompt_version}` |",
        f"| Review prompt | `{versions.review_prompt_version}` |",
        f"| Orchestrator | `{versions.orchestrator_version}` |",
        f"| Primary model | `{versions.primary_model or UNRECORDED}` |",
        f"| Reviewer model | `{versions.reviewer_model or UNRECORDED}` |",
        "",
        "## Recorded facts",
        "",
    ]
    lines.extend(_facts_table(retrospective.run_facts))
    lines.extend(["## Observations Mailman recorded", ""])
    if retrospective.observations:
        for observation in retrospective.observations:
            lines.extend(_observation_block(observation))
    else:
        lines.extend(
            [
                "Mailman observed nothing worth recording on its own. That is",
                "not the same as a run with nothing to learn from.",
                "",
            ]
        )
    lines.extend(
        [
            "## Questions to answer",
            "",
            "Answer with observations. An empty section means the question was",
            "considered and had no answer, so write `none` rather than",
            "deleting the heading.",
            "",
        ]
    )
    for section in RETROSPECTIVE_SECTIONS:
        lines.extend(
            [
                f"### {section.title}",
                "",
                section.question,
                "",
                f"Suggested channel `{section.channel}`, category "
                f"`{section.category}`.",
                "",
                "-",
                "",
            ]
        )
    lines.extend(
        [
            "## Promotion",
            "",
            "A lesson leaves this file only along the registry path:",
            "",
            "```text",
            "OBSERVATION -> HYPOTHESIS -> CANDIDATE_LESSON -> VALIDATED ->",
            "PROMOTED_TO_SKILL",
            "```",
            "",
            "Validation needs evidence from at least two distinct runs and at",
            "least one observation a machine or a human made. An agent's own",
            "account of its work cannot validate a rule about that agent.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
