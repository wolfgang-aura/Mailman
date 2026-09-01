from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from mailman.knowledge.taxonomy import (
    OBSERVED_EVIDENCE_WEIGHT,
    EvidenceChannel,
    KnowledgeLayer,
    PatternCategory,
    Scope,
    channel_weight,
)
from mailman.models import utc_now


LESSON_SCHEMA_VERSION = 1

# A vivid single run is a story. Two runs are the beginning of a pattern.
MINIMUM_VALIDATION_RUNS = 2

_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class LessonState(StrEnum):
    OBSERVATION = "OBSERVATION"
    HYPOTHESIS = "HYPOTHESIS"
    CANDIDATE_LESSON = "CANDIDATE_LESSON"
    VALIDATED = "VALIDATED"
    PROMOTED_TO_SKILL = "PROMOTED_TO_SKILL"
    REFINED = "REFINED"
    REJECTED = "REJECTED"


ALLOWED_LESSON_TRANSITIONS: dict[LessonState, frozenset[LessonState]] = {
    LessonState.OBSERVATION: frozenset(
        {LessonState.HYPOTHESIS, LessonState.REJECTED}
    ),
    LessonState.HYPOTHESIS: frozenset(
        {LessonState.CANDIDATE_LESSON, LessonState.REFINED, LessonState.REJECTED}
    ),
    LessonState.CANDIDATE_LESSON: frozenset(
        {LessonState.VALIDATED, LessonState.REFINED, LessonState.REJECTED}
    ),
    LessonState.VALIDATED: frozenset(
        {
            LessonState.PROMOTED_TO_SKILL,
            LessonState.REFINED,
            LessonState.REJECTED,
        }
    ),
    # A promoted rule can still be contradicted by a later run.
    LessonState.PROMOTED_TO_SKILL: frozenset(
        {LessonState.REFINED, LessonState.REJECTED}
    ),
    LessonState.REFINED: frozenset(
        {
            LessonState.HYPOTHESIS,
            LessonState.CANDIDATE_LESSON,
            LessonState.REJECTED,
        }
    ),
    # Terminal. Reviving a rejected idea means opening a new lesson with the
    # evidence that changed the answer.
    LessonState.REJECTED: frozenset(),
}


@dataclass(frozen=True)
class LessonEvidence:
    """One run's support for a lesson, and what found it."""

    run_id: str
    channel: EvidenceChannel
    observation_id: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("lesson evidence requires a run ID")

    @property
    def weight(self) -> int:
        return channel_weight(self.channel)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "channel": str(self.channel),
            "observation_id": self.observation_id,
            "note": self.note,
            "weight": self.weight,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LessonEvidence:
        return cls(
            run_id=data["run_id"],
            channel=EvidenceChannel(data["channel"]),
            observation_id=data.get("observation_id"),
            note=data.get("note", ""),
        )


@dataclass
class LessonEntry:
    """A candidate rule on its way to the skill, or on its way to rejection."""

    lesson_id: str
    category: PatternCategory
    observation: str
    hypothesis: str = ""
    guidance: str = ""
    layer: KnowledgeLayer = KnowledgeLayer.CORE
    conditions: list[str] = field(default_factory=list)
    scope: Scope = Scope.RUN_ONLY
    state: LessonState = LessonState.OBSERVATION
    evidence: list[LessonEvidence] = field(default_factory=list)
    validation: list[str] = field(default_factory=list)
    schema_version: int = LESSON_SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    history: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not _IDENTIFIER_PATTERN.fullmatch(self.lesson_id):
            raise ValueError(
                "lesson_id must start with a letter or digit and use only "
                "letters, digits, underscores, hyphens, or periods"
            )
        if not self.observation.strip():
            raise ValueError("a lesson needs the observation it came from")
        self._check_layer()

    def _check_layer(self) -> None:
        if self.layer is KnowledgeLayer.CONDITIONAL and not self.conditions:
            raise ValueError(
                "conditional knowledge must name the conditions it applies under"
            )
        if self.layer is KnowledgeLayer.CORE and self.conditions:
            raise ValueError(
                "core knowledge applies to every run and cannot carry conditions"
            )

    # Evidence ----------------------------------------------------------

    def add_evidence(self, evidence: LessonEvidence) -> None:
        self.evidence.append(evidence)
        self.updated_at = utc_now()

    def run_ids(self) -> set[str]:
        return {entry.run_id for entry in self.evidence}

    def strongest_weight(self) -> int:
        return max((entry.weight for entry in self.evidence), default=0)

    def is_observed(self) -> bool:
        """Report whether any evidence came from a machine or a human."""
        return self.strongest_weight() >= OBSERVED_EVIDENCE_WEIGHT

    # State -------------------------------------------------------------

    def _check_content(self, target: LessonState) -> None:
        if target is LessonState.HYPOTHESIS and not self.hypothesis.strip():
            raise ValueError("a hypothesis is required before leaving OBSERVATION")
        if target is LessonState.CANDIDATE_LESSON and not self.guidance.strip():
            raise ValueError(
                "candidate guidance is required before a lesson becomes a "
                "candidate"
            )
        if target is not LessonState.VALIDATED:
            return
        supporting_runs = len(self.run_ids())
        if supporting_runs < MINIMUM_VALIDATION_RUNS:
            raise ValueError(
                f"validation needs evidence from at least "
                f"{MINIMUM_VALIDATION_RUNS} distinct runs, found "
                f"{supporting_runs}"
            )
        if not self.is_observed():
            raise ValueError(
                "validation needs at least one observation made by a machine "
                "or a human, not only agent self-report"
            )
        if not self.validation:
            raise ValueError("validation needs at least one recorded result")

    def transition(self, target: LessonState, reason: str) -> None:
        if target not in ALLOWED_LESSON_TRANSITIONS[self.state]:
            raise ValueError(f"invalid lesson transition: {self.state} -> {target}")
        self._check_content(target)
        previous = self.state
        self.state = target
        self.updated_at = utc_now()
        self.history.append(
            {
                "at": self.updated_at,
                "from": str(previous),
                "to": str(target),
                "reason": reason,
            }
        )

    def refine(self, *, guidance: str, reason: str) -> None:
        """Narrow the guidance and send the lesson back to re-earn its state."""
        self.transition(LessonState.REFINED, reason)
        self.guidance = guidance

    # Serialization -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "lesson_id": self.lesson_id,
            "category": str(self.category),
            "observation": self.observation,
            "hypothesis": self.hypothesis,
            "guidance": self.guidance,
            "layer": str(self.layer),
            "conditions": list(self.conditions),
            "scope": str(self.scope),
            "state": str(self.state),
            "evidence": [entry.to_dict() for entry in self.evidence],
            "validation": list(self.validation),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LessonEntry:
        return cls(
            lesson_id=data["lesson_id"],
            category=PatternCategory(data["category"]),
            observation=data["observation"],
            hypothesis=data.get("hypothesis", ""),
            guidance=data.get("guidance", ""),
            layer=KnowledgeLayer(data.get("layer", KnowledgeLayer.CORE)),
            conditions=list(data.get("conditions", [])),
            scope=Scope(data.get("scope", Scope.RUN_ONLY)),
            state=LessonState(data.get("state", LessonState.OBSERVATION)),
            evidence=[
                LessonEvidence.from_dict(entry) for entry in data.get("evidence", [])
            ],
            validation=list(data.get("validation", [])),
            schema_version=data.get("schema_version", LESSON_SCHEMA_VERSION),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            history=list(data.get("history", [])),
        )


@dataclass
class LessonRegistry:
    """The intermediate store between a run and the engineering skill."""

    lessons: list[LessonEntry] = field(default_factory=list)
    schema_version: int = LESSON_SCHEMA_VERSION

    def add(self, lesson: LessonEntry) -> LessonEntry:
        if any(entry.lesson_id == lesson.lesson_id for entry in self.lessons):
            raise ValueError(f"duplicate lesson ID: {lesson.lesson_id}")
        self.lessons.append(lesson)
        return lesson

    def get(self, lesson_id: str) -> LessonEntry:
        for lesson in self.lessons:
            if lesson.lesson_id == lesson_id:
                return lesson
        raise KeyError(lesson_id)

    def by_state(self, state: LessonState) -> list[LessonEntry]:
        return [lesson for lesson in self.lessons if lesson.state is state]

    def by_category(self, category: PatternCategory) -> list[LessonEntry]:
        return [lesson for lesson in self.lessons if lesson.category is category]

    def next_lesson_id(self) -> str:
        return f"LESSON-{len(self.lessons) + 1:04d}"

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for lesson in self.lessons:
            key = str(lesson.state)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "lessons": [lesson.to_dict() for lesson in self.lessons],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LessonRegistry:
        schema_version = data.get("schema_version", LESSON_SCHEMA_VERSION)
        if schema_version != LESSON_SCHEMA_VERSION:
            raise ValueError(f"unsupported lesson registry schema: {schema_version}")
        return cls(
            lessons=[
                LessonEntry.from_dict(entry) for entry in data.get("lessons", [])
            ],
            schema_version=schema_version,
        )


def default_registry_path() -> Path:
    return Path.cwd() / "knowledge" / "lessons.json"


def load_registry(path: Path | None = None) -> LessonRegistry:
    """Load the registry, treating a missing file as an empty one."""
    registry_path = path or default_registry_path()
    if not registry_path.is_file():
        return LessonRegistry()
    return LessonRegistry.from_dict(
        json.loads(registry_path.read_text(encoding="utf-8"))
    )


def write_registry(registry: LessonRegistry, path: Path | None = None) -> Path:
    destination = path or default_registry_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(registry.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def _yaml_scalar(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("\n", " ").strip()
    return f'"{escaped}"'


@dataclass(frozen=True)
class SkillRuleProvenance:
    """Why a rule is in the skill, rendered for `references/provenance.md`."""

    rule_id: str
    rule: str
    reason: str
    introduced: str
    skill_version: str
    failure_pattern: PatternCategory
    expected_change: str
    motivating_runs: tuple[str, ...] = ()
    validation: tuple[str, ...] = ()
    lesson_id: str | None = None
    follow_up: str = ""

    def __post_init__(self) -> None:
        if not self.rule.strip():
            raise ValueError("a promoted rule needs its instruction text")
        if not self.expected_change.strip():
            raise ValueError(
                "a promoted rule needs the behavior change it is expected to "
                "cause, or later runs cannot test it"
            )
        if not self.motivating_runs:
            raise ValueError("a promoted rule needs the runs that motivated it")

    def to_yaml_block(self) -> str:
        lines = [
            "```yaml",
            f"rule_id: {self.rule_id}",
            f"rule: {_yaml_scalar(self.rule)}",
            f"reason: {_yaml_scalar(self.reason)}",
            f"failure_pattern: {self.failure_pattern}",
            f"expected_change: {_yaml_scalar(self.expected_change)}",
            "evidence:",
        ]
        lines.extend(f"  - {run_id}" for run_id in self.motivating_runs)
        lines.append(f"introduced: {self.introduced}")
        lines.append(f"skill_version: {self.skill_version}")
        if self.lesson_id:
            lines.append(f"lesson_id: {self.lesson_id}")
        if self.validation:
            lines.append("validation:")
            lines.extend(f"  - {_yaml_scalar(item)}" for item in self.validation)
        else:
            lines.append("validation: []")
        lines.append(f"follow_up: {_yaml_scalar(self.follow_up)}")
        lines.append("```")
        return "\n".join(lines) + "\n"

    @classmethod
    def from_lesson(
        cls,
        lesson: LessonEntry,
        *,
        rule_id: str,
        introduced: str,
        skill_version: str,
        expected_change: str,
        follow_up: str = "",
    ) -> SkillRuleProvenance:
        """Build provenance for a lesson that has already been validated."""
        if lesson.state is not LessonState.VALIDATED:
            raise ValueError(
                f"only a VALIDATED lesson may be promoted, found {lesson.state}"
            )
        return cls(
            rule_id=rule_id,
            rule=lesson.guidance,
            reason=lesson.observation,
            introduced=introduced,
            skill_version=skill_version,
            failure_pattern=lesson.category,
            expected_change=expected_change,
            motivating_runs=tuple(sorted(lesson.run_ids())),
            validation=tuple(lesson.validation),
            lesson_id=lesson.lesson_id,
            follow_up=follow_up,
        )
