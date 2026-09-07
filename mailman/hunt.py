"""Persistent PRHunt quota and completion gates, shared by every coordinator."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from mailman.agents import normalize_agent_name
from mailman.artifacts import load_run, new_run_id
from mailman.completion import finalize_review, read_object
from mailman.handoff import check_handoff, load_handoff
from mailman.models import RunStatus, utc_now
from mailman.review_decision import DecisionError, load_decision
from mailman.screen import load_screen
from mailman.target_intel import repository_slug
from mailman.targeting import assess_target

PROCEDURE = Path(__file__).with_name("procedure.md")
HUMAN_REASONS = ("authentication", "budget", "scope", "conflicting-instructions")
DROP_CODES = {
    "issue-assigned", "work-handed-over", "open-pull-request",
    "already-fixed-upstream", "bug-not-reproduced", "fails-freshness-bar",
}


def save(path: Path, record: dict) -> None:
    record["updated_at"] = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def hunt_path(root: Path, hunt_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", hunt_id):
        raise ValueError("invalid hunt ID")
    return root.parent / "hunts" / hunt_id / "hunt.json"


def create_hunt(root: Path, count: int, *, primary: str, primary_model: str,
                reviewer: str, reviewer_model: str) -> dict:
    if count < 1 or isinstance(count, bool):
        raise ValueError("the PR count must be positive")
    if not primary_model.strip() or not reviewer_model.strip():
        raise ValueError("ask for both model IDs before starting the hunt")
    record = {
        "schema_version": 1, "hunt_id": new_run_id(), "requested": count,
        "data_root": str(root.resolve()), "created_at": utc_now(),
        "primary": {"agent": normalize_agent_name(primary), "model": primary_model},
        "reviewer": {"agent": normalize_agent_name(reviewer), "model": reviewer_model},
        "procedure_sha256": hashlib.sha256(PROCEDURE.read_bytes()).hexdigest(),
        "runs": [], "escalations": [], "status": "RUNNING",
    }
    save(hunt_path(root, record["hunt_id"]), record)
    return record


def load_hunt(root: Path, hunt_id: str) -> dict:
    record = read_object(hunt_path(root, hunt_id))
    if not record or record.get("data_root") != str(root.resolve()):
        raise ValueError("hunt missing or belongs to another data root")
    if record.get("procedure_sha256") != hashlib.sha256(PROCEDURE.read_bytes()).hexdigest():
        raise ValueError("procedure changed: read `mailman procedure`, then use `hunt refresh-procedure`")
    return record


def add_run(root: Path, record: dict, run_id: str) -> None:
    run, _ = load_run(run_id, root)
    for role in ("primary", "reviewer"):
        config = getattr(run, role)
        if {"agent": config.agent, "model": config.model} != record[role]:
            raise ValueError(f"{role} differs from the hunt's selected model; do not substitute models")
    if any(row["run_id"] == run_id for row in record["runs"]):
        return
    record["runs"].append({"run_id": run_id})
    save(hunt_path(root, record["hunt_id"]), record)


def next_action(directory: Path) -> dict:
    run, _ = load_run(directory.name, directory.parent)

    def action(stage: str, command: str, detail: str = "", disposition: str = "REPAIR") -> dict:
        return {"run_id": run.run_id, "ready": False, "stage": stage,
                "action": command, "detail": detail, "disposition": disposition,
                "human_required": False}

    screen = load_screen(directory.parent, repository_slug(run.repository)) or {}
    if screen.get("verdict") != "pass" or not screen.get("success"):
        return action("screen", f"mailman screen-target {repository_slug(run.repository)} --refresh",
                      disposition="REPLACE" if screen.get("success") else "REPAIR")
    for filename, command in (
        ("issue.json", "fetch-issue"), ("duplicate-search.json", "duplicate-search"),
        ("prior-art.json", "prior-art"), ("target-intel.json", "target-intel"),
        ("claims.json", "claims"), ("workspace.json", "prepare-workspace"),
        ("environment.json", "prepare-environment"), ("reproduction.json", "reproduce"),
    ):
        data = read_object(directory / filename)
        if not data or data.get("success") is False:
            return action(command, f"mailman {command} {run.run_id}")
    assessment = assess_target(directory)
    if assessment.blocking:
        code = assessment.blocking[0]
        return action("target", f"mailman check-target {run.run_id}", "; ".join(assessment.blocking),
                      "REPLACE" if code in DROP_CODES else "REPAIR")
    if not read_object(directory / "prompts.json").get("verification_command"):
        return action("prompts", f"mailman build-prompts {run.run_id} -- EXECUTABLE ARG ...")
    if run.status not in (RunStatus.ENGINEERING_COMPLETE, RunStatus.READY_FOR_HUMAN_REVIEW):
        history = read_object(directory / "orchestration.json")
        has_primary = any(step.get("name") == "agent:primary" for step in history.get("steps", []))
        command = "resume-review" if run.status is RunStatus.BLOCKED and has_primary else "orchestrate"
        return action("engineering", f"mailman {command} {run.run_id}",
                      "Read orchestration.json and the failed stage before retrying." if run.status is RunStatus.BLOCKED else "")
    exported_diff = directory / "export" / "changes.diff"
    exported = read_object(directory / "export" / "export.json")
    from mailman.completion import candidate_digest
    if (not exported_diff.is_file() or not exported.get("candidate_digest")
            or exported["candidate_digest"] != candidate_digest(Path(exported["workspace"]), run.base_commit)):
        return action("export", f"mailman export-patch {run.run_id}")
    submission = read_object(directory / "submission" / "submission.json")
    if (not submission.get("ready")
            or submission.get("diff_sha256") != hashlib.sha256(exported_diff.read_text(encoding="utf-8").encode("utf-8")).hexdigest()):
        return action("submission", f"mailman prepare-submission {run.run_id} --policy POLICY.json",
                      "; ".join(submission.get("blocking_codes", [])))
    try:
        decision = load_decision(directory)
    except DecisionError as error:
        return action("decision", f"mailman decision {run.run_id}", str(error))
    if decision.recommendation != "SEND":
        return action("decision", "Resolve the HOLD or replace the candidate.", disposition="REPLACE" if decision.recommendation == "DROP" else "REPAIR")
    if decision.blocking_questions:
        return action("decision", "Resolve coordinator work; record a genuine user dependency with hunt escalate.")
    try:
        finalize_review(directory)
    except (OSError, ValueError) as error:
        return action("finalize", f"mailman finalize-review {run.run_id}", str(error))
    handoff = load_handoff(directory)
    if not handoff or handoff.get("kind") != "pull-request":
        return action("handoff", f"mailman handoff {run.run_id} --body FINAL_BODY --repo OWNER/REPO --head OWNER:BRANCH --base BASE --title TITLE")
    if repository_slug(handoff.get("repository", "")) != repository_slug(run.repository):
        return action("handoff", "Correct the handoff destination to the run repository.")
    if handoff.get("first_person_claims") or handoff.get("head_owner_type") != "User":
        return action("handoff", "Remove unsupported personal claims and use a confirmed personal fork.")
    checked = check_handoff(directory)
    if not checked["ok"]:
        return action("handoff", f"mailman handoff-check {run.run_id}", checked["detail"])
    return {"run_id": run.run_id, "ready": True, "stage": "filing-approval",
            "disposition": "READY", "human_required": False,
            "action": "Include in the final approval packet."}


def status(root: Path, record: dict) -> dict:
    rows = []
    targets = set()
    for row in record["runs"]:
        if row.get("dropped"):
            rows.append({**row, "ready": False, "disposition": "REPLACED", "human_required": False})
            continue
        run, directory = load_run(row["run_id"], root)
        try:
            checked = next_action(directory)
        except (OSError, ValueError) as error:
            checked = {"run_id": run.run_id, "ready": False, "disposition": "REPAIR",
                       "human_required": False, "detail": str(error), "action": "Repair the named evidence record."}
        key = (repository_slug(run.repository), run.issue or run.defect_report)
        if checked["ready"] and key in targets:
            checked.update(ready=False, disposition="REPLACE", detail="same target already counted")
        if checked["ready"]:
            targets.add(key)
        rows.append(checked)
    ready = sum(row["ready"] for row in rows)
    result = {"hunt_id": record["hunt_id"], "requested": record["requested"],
              "ready": ready, "remaining": max(0, record["requested"] - ready),
              "checked_at": utc_now(), "runs": rows,
              "next": "Prepare the approval packet." if ready >= record["requested"] else "Complete the next action or find a replacement candidate.",
              "escalations": record["escalations"]}
    record["last_check"] = result
    save(hunt_path(root, record["hunt_id"]), record)
    return result


def finish(root: Path, record: dict) -> dict:
    from mailman.review_packet import write_packet_page
    from mailman.review_page import write_run_page
    result = status(root, record)
    if result["remaining"]:
        return {**result, "complete": False}
    directories = [root / row["run_id"] for row in result["runs"] if row["ready"]][:record["requested"]]
    for directory in directories:
        write_run_page(directory)
    packet = hunt_path(root, record["hunt_id"]).parent / "index.html"
    write_packet_page(directories, packet, title="PRs awaiting filing approval")
    record["status"] = "AWAITING_FILING_APPROVAL"
    record["packet"] = str(packet)
    record["packet_sha256"] = hashlib.sha256(packet.read_bytes()).hexdigest()
    save(hunt_path(root, record["hunt_id"]), record)
    return {**result, "complete": True, "packet": str(packet), "published": False}
