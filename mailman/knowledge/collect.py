from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

from mailman import __version__
from mailman.knowledge.retrospective import (
    UNRECORDED,
    Retrospective,
    RunVersions,
    render_retrospective,
)
from mailman.knowledge.taxonomy import (
    EvidenceChannel,
    Outcome,
    PatternCategory,
    Scope,
)
from mailman.models import RunRecord, RunStatus
from mailman.reproduction import not_reproductions


RETROSPECTIVE_JSON = "retrospective.json"
RETROSPECTIVE_MARKDOWN = "retrospective.md"

DEFAULT_SKILL_PATH = Path("skills") / "open-source-engineering" / "SKILL.md"

# The orchestration step names Mailman writes, mapped to where a failure at that
# step belongs in the taxonomy.
_STEP_CATEGORIES: dict[str, PatternCategory] = {
    "workspace": PatternCategory.ENVIRONMENT,
    "agent:primary": PatternCategory.IMPLEMENTATION,
    "agent:reviewer": PatternCategory.REVIEW,
    "verdict": PatternCategory.REVIEW,
    "verification:primary": PatternCategory.VERIFICATION,
    "verification:revision": PatternCategory.VERIFICATION,
    "verification:final": PatternCategory.VERIFICATION,
}

# A reviewer process that failed is a harness observation, not a review finding.
_STEP_CHANNELS: dict[str, EvidenceChannel] = {
    "agent:primary": EvidenceChannel.PRIMARY_AGENT_FAILURE,
    "verdict": EvidenceChannel.REVIEWER_FINDING,
}

_NO_REPORT_ASSUMPTION = (
    "A process that exits zero has done the work it was asked to do."
)


def _read_json(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _prompt_version(run_directory: Path, role: str) -> str:
    """Digest the last prompt of a role, the way `toolchain.json` pins tools."""
    prompts = sorted((run_directory / "prompts").glob(f"*-{role}.md"))
    if not prompts:
        return UNRECORDED
    digest = sha256(prompts[-1].read_bytes()).hexdigest()
    return f"sha256:{digest}"


def _skill_version(skill_path: Path | None) -> str:
    """Read a `version:` key from the skill front matter when one exists."""
    path = skill_path if skill_path is not None else Path.cwd() / DEFAULT_SKILL_PATH
    if not path.is_file():
        return UNRECORDED
    for line in path.read_text(encoding="utf-8").splitlines()[:20]:
        if line.startswith("version:"):
            value = line.split(":", 1)[1].strip()
            if value:
                return value
    return "unversioned"


def _versions(
    run: RunRecord, run_directory: Path, skill_path: Path | None
) -> RunVersions:
    return RunVersions(
        skill_version=_skill_version(skill_path),
        primary_prompt_version=_prompt_version(run_directory, "primary"),
        review_prompt_version=_prompt_version(run_directory, "reviewer"),
        orchestrator_version=__version__,
        primary_model=run.primary.model,
        reviewer_model=run.reviewer.model,
    )


def _run_facts(
    run: RunRecord,
    verifications: list[dict[str, Any]],
    executions: list[tuple[str, dict[str, Any]]],
    orchestration: dict[str, Any] | None,
) -> dict[str, Any]:
    failed_verifications = [
        index + 1
        for index, record in enumerate(verifications)
        if record.get("timed_out") or record.get("exit_code") != 0
    ]
    facts: dict[str, Any] = {
        "primary_agent": run.primary.agent,
        "reviewer_agent": run.reviewer.agent,
        "review_cycles": run.review_cycles,
        "human_decision": run.human_decision or "none recorded",
        "verification_runs": len(verifications),
        "failed_verification_records": failed_verifications or "none",
        "agent_executions": len(executions),
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }
    if orchestration is not None:
        steps = orchestration.get("steps", [])
        facts["orchestration_steps"] = len(steps)
        facts["revisions_used"] = orchestration.get("revisions_used")
        facts["failed_steps"] = [
            step.get("name") for step in steps if not step.get("ok", True)
        ] or "none"
    return facts


def _load_executions(run_directory: Path) -> list[tuple[str, dict[str, Any]]]:
    directory = run_directory / "agent-executions"
    if not directory.is_dir():
        return []
    executions: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        record = _read_json(path)
        if isinstance(record, dict):
            executions.append((path.name, record))
    return executions


def _seed_from_orchestration(
    retrospective: Retrospective, steps: list[dict[str, Any]]
) -> None:
    last_failed_category = PatternCategory.ENVIRONMENT
    for step in steps:
        if step.get("ok", True):
            continue
        name = str(step.get("name", ""))
        detail = str(step.get("detail", ""))
        data = step.get("data", {}) if isinstance(step.get("data"), dict) else {}
        if name == "blocked":
            retrospective.add_observation(
                channel=EvidenceChannel.AUTOMATED_VERIFICATION,
                category=last_failed_category,
                outcome=Outcome.FAILURE,
                summary=f"Mailman blocked the run: {detail}",
                evidence=("orchestration.json",),
            )
            continue
        category = _STEP_CATEGORIES.get(name, PatternCategory.TOOL_USAGE)
        channel = _STEP_CHANNELS.get(name, EvidenceChannel.AUTOMATED_VERIFICATION)
        assumption = None
        if data.get("report_present") is False and data.get("exit_code") == 0:
            # The failure mode the first promoted skill rule exists for.
            category = PatternCategory.TOOL_USAGE
            assumption = _NO_REPORT_ASSUMPTION
        last_failed_category = category
        retrospective.add_observation(
            channel=channel,
            category=category,
            outcome=Outcome.FAILURE,
            summary=f"Step `{name}` did not pass: {detail}",
            assumption=assumption,
            evidence=("orchestration.json",),
        )
    for step in steps:
        if step.get("name") != "verdict":
            continue
        data = step.get("data", {}) if isinstance(step.get("data"), dict) else {}
        if data.get("verdict") == "REVISE":
            retrospective.add_observation(
                channel=EvidenceChannel.REVIEWER_FINDING,
                category=PatternCategory.REVIEW,
                outcome=Outcome.FAILURE,
                summary="The reviewer required a revision of the candidate.",
                detail=(
                    "Read the review report for the findings. Record whether "
                    "each was correct, including any the primary agent rebutted."
                ),
                evidence=("reviewer-report.md", "orchestration.json"),
            )


def _seed_from_records(
    retrospective: Retrospective,
    verifications: list[dict[str, Any]],
    executions: list[tuple[str, dict[str, Any]]],
) -> None:
    for index, record in enumerate(verifications, start=1):
        if not record.get("timed_out") and record.get("exit_code") == 0:
            continue
        detail = (
            "timed out"
            if record.get("timed_out")
            else f"exited with code {record.get('exit_code')}"
        )
        retrospective.add_observation(
            channel=EvidenceChannel.AUTOMATED_VERIFICATION,
            category=PatternCategory.VERIFICATION,
            outcome=Outcome.FAILURE,
            summary=f"Verification record {index} {detail}.",
            evidence=(f"commands/{index:04d}.json",),
        )
    for name, record in executions:
        role = str(record.get("role", "unknown"))
        process = record.get("process", {})
        process = process if isinstance(process, dict) else {}
        timed_out = bool(process.get("timed_out"))
        exit_code = process.get("exit_code")
        report_present = bool(record.get("report_present"))
        if not timed_out and exit_code == 0 and report_present:
            continue
        channel = (
            EvidenceChannel.PRIMARY_AGENT_FAILURE
            if role == "primary"
            else EvidenceChannel.AUTOMATED_VERIFICATION
        )
        if timed_out:
            summary = f"The {role} agent timed out."
            category = PatternCategory.ENVIRONMENT
            assumption = None
        elif not report_present and exit_code == 0:
            summary = f"The {role} agent exited zero without writing a report."
            category = PatternCategory.TOOL_USAGE
            assumption = _NO_REPORT_ASSUMPTION
        else:
            summary = f"The {role} agent exited with code {exit_code}."
            category = PatternCategory.TOOL_USAGE
            assumption = None
        retrospective.add_observation(
            channel=channel,
            category=category,
            outcome=Outcome.FAILURE,
            summary=summary,
            assumption=assumption,
            evidence=(f"agent-executions/{name}",),
        )


def collect_retrospective(
    run: RunRecord,
    run_directory: Path,
    *,
    skill_path: Path | None = None,
) -> Retrospective:
    """Draft a retrospective from what Mailman recorded, and nothing else."""
    verifications = _read_json(run_directory / "verification.json")
    # A reproduction is recorded in the same stream and is expected to fail.
    # Counting one here would seed a failed-verification observation for a gate
    # that did exactly what it was asked to do.
    verifications = not_reproductions(
        [record for record in (verifications or []) if isinstance(record, dict)]
    )
    executions = _load_executions(run_directory)
    orchestration = _read_json(run_directory / "orchestration.json")
    orchestration = orchestration if isinstance(orchestration, dict) else None

    retrospective = Retrospective(
        run_id=run.run_id,
        repository=run.repository,
        issue=run.issue,
        base_commit=run.base_commit,
        run_status=str(run.status),
        versions=_versions(run, run_directory, skill_path),
        run_facts=_run_facts(run, verifications, executions, orchestration),
    )

    # Prefer the orchestration record when there is one: it already explains
    # which stage failed, so the raw records would only repeat it.
    if orchestration is not None:
        steps = [
            step for step in orchestration.get("steps", []) if isinstance(step, dict)
        ]
        _seed_from_orchestration(retrospective, steps)
    else:
        _seed_from_records(retrospective, verifications, executions)

    if run.status is RunStatus.READY_FOR_HUMAN_REVIEW:
        revisions = (
            orchestration.get("revisions_used") if orchestration is not None else None
        )
        summary = "The run reached READY_FOR_HUMAN_REVIEW."
        if revisions == 0:
            summary = (
                "The run reached READY_FOR_HUMAN_REVIEW with no revision, and "
                "verification passed after the primary stage and again at the end."
            )
        retrospective.add_observation(
            channel=EvidenceChannel.AUTOMATED_VERIFICATION,
            category=PatternCategory.VERIFICATION,
            outcome=Outcome.SUCCESS,
            summary=summary,
            detail=(
                "Reaching a human is not the same as being right. Record what "
                "the human decides."
            ),
            scope=Scope.RUN_ONLY,
            evidence=("run.json",),
        )
    elif run.status is RunStatus.BLOCKED and not retrospective.observations:
        retrospective.add_observation(
            channel=EvidenceChannel.AUTOMATED_VERIFICATION,
            category=PatternCategory.ENVIRONMENT,
            outcome=Outcome.FAILURE,
            summary="The run is BLOCKED and left no step evidence to explain it.",
            evidence=("run.json",),
        )
    return retrospective


def write_retrospective(
    retrospective: Retrospective,
    run_directory: Path,
    *,
    force: bool = False,
) -> tuple[Path, Path]:
    """Write both retrospective files, refusing to discard human writing."""
    json_path = run_directory / RETROSPECTIVE_JSON
    markdown_path = run_directory / RETROSPECTIVE_MARKDOWN
    if not force:
        for path in (json_path, markdown_path):
            if path.exists():
                raise ValueError(
                    f"{path.name} already exists; pass --force to replace it, "
                    "which discards anything written into it by hand"
                )
    temporary = json_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(retrospective.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(json_path)
    markdown_path.write_text(
        render_retrospective(retrospective), encoding="utf-8"
    )
    return json_path, markdown_path
