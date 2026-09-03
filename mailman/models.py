from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class RunStatus(StrEnum):
    INITIALIZED = "INITIALIZED"
    PRIMARY_RUNNING = "PRIMARY_RUNNING"
    REVIEW_PENDING = "REVIEW_PENDING"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    VERIFICATION_PENDING = "VERIFICATION_PENDING"
    READY_FOR_HUMAN_REVIEW = "READY_FOR_HUMAN_REVIEW"
    BLOCKED = "BLOCKED"
    ABANDONED = "ABANDONED"


ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.INITIALIZED: frozenset(
        {RunStatus.PRIMARY_RUNNING, RunStatus.BLOCKED, RunStatus.ABANDONED}
    ),
    RunStatus.PRIMARY_RUNNING: frozenset(
        {
            RunStatus.REVIEW_PENDING,
            # The primary stage verifies its own work before the reviewer sees
            # it. A failure there asks for the same revision a reviewer would.
            RunStatus.REVISION_REQUIRED,
            RunStatus.BLOCKED,
            RunStatus.ABANDONED,
        }
    ),
    RunStatus.REVIEW_PENDING: frozenset(
        {
            RunStatus.REVISION_REQUIRED,
            RunStatus.VERIFICATION_PENDING,
            RunStatus.BLOCKED,
            RunStatus.ABANDONED,
        }
    ),
    RunStatus.REVISION_REQUIRED: frozenset(
        {RunStatus.REVIEW_PENDING, RunStatus.BLOCKED, RunStatus.ABANDONED}
    ),
    RunStatus.VERIFICATION_PENDING: frozenset(
        {
            RunStatus.READY_FOR_HUMAN_REVIEW,
            RunStatus.REVISION_REQUIRED,
            RunStatus.BLOCKED,
            RunStatus.ABANDONED,
        }
    ),
    RunStatus.READY_FOR_HUMAN_REVIEW: frozenset(),
    RunStatus.BLOCKED: frozenset(
        {RunStatus.PRIMARY_RUNNING, RunStatus.REVIEW_PENDING, RunStatus.ABANDONED}
    ),
    RunStatus.ABANDONED: frozenset(),
}


@dataclass(frozen=True)
class AgentConfig:
    agent: str
    model: str | None = None
    configuration: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunRecord:
    run_id: str
    repository: str
    # One of these two carries the defect. `issue` is an upstream GitHub issue;
    # `defect_report` is a file the operator wrote because the target has no
    # usable tracker. See
    # https://github.com/wolfgang-aura/Mailman/issues/45.
    issue: str | None
    base_commit: str
    primary: AgentConfig
    reviewer: AgentConfig
    status: RunStatus = RunStatus.INITIALIZED
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    defect_report: str | None = None
    review_cycles: int = 0
    human_decision: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)

    def transition(self, target: RunStatus, reason: str) -> None:
        if target not in ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(f"invalid transition: {self.status} -> {target}")
        previous = self.status
        self.status = target
        self.updated_at = utc_now()
        self.history.append(
            {
                "at": self.updated_at,
                "from": str(previous),
                "to": str(target),
                "reason": reason,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = str(self.status)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        primary = AgentConfig(**data["primary"])
        reviewer = AgentConfig(**data["reviewer"])
        return cls(
            run_id=data["run_id"],
            repository=data["repository"],
            issue=data.get("issue"),
            base_commit=data["base_commit"],
            primary=primary,
            reviewer=reviewer,
            defect_report=data.get("defect_report"),
            status=RunStatus(data["status"]),
            schema_version=data.get("schema_version", SCHEMA_VERSION),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            review_cycles=data.get("review_cycles", 0),
            human_decision=data.get("human_decision"),
            history=data.get("history", []),
        )
