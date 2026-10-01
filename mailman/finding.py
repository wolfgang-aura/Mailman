"""The record of a defect found by hunting rather than taken from a tracker.

A finding carries its reproducer and the conditions the defect needs, and says
for each condition whether this host can supply it. On 2026-09-06 a rename that
differed only in case deleted the file it had just written: the defect needs a
case-insensitive filesystem and a case-sensitive path comparison, and the
Windows host supplied the first and not the second, so the reproducer modelled
the second. Without a record that distinction lived only in a transcript.
Mailman #54.

`init-run --defect-report finding.json` accepts this record, and the briefing
the agents read renders the conditions table from it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

FINDING_FILENAME = "finding.json"
FINDING_KIND = "mailman-finding"
FINDING_SCHEMA_VERSION = 1
#: What a condition's `host_satisfies` may say. `unknown` is honest when the
#: check could not be made; it is not a default to leave in place.
HOST_SATISFIES = (True, False, "unknown")
_COMMIT = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


class FindingError(ValueError):
    """A finding record that cannot be used, with every problem named."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid finding record: " + "; ".join(problems))


def blank_finding(repository: str = "", base_commit: str = "") -> dict[str, Any]:
    """A template that fails validation until every field is filled in."""
    return {
        "kind": FINDING_KIND,
        "schema_version": FINDING_SCHEMA_VERSION,
        "title": "",
        "summary": "",
        "repository": repository,
        "base_commit": base_commit,
        "reproducer": {"command": [], "script": "", "expected": ""},
        "conditions": [
            {
                "name": "",
                "required": True,
                "host_satisfies": "unknown",
                "how_checked": "",
                "modelled_by": "",
            }
        ],
        "evidence": {},
    }


def is_finding_file(path: Path) -> bool:
    """Whether `path` is a finding record rather than a prose defect report."""
    if path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("kind") == FINDING_KIND


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_finding(data: Any) -> dict[str, Any]:
    """Return the record unchanged, or raise FindingError naming each problem."""
    if not isinstance(data, dict):
        raise FindingError(["the record must be a JSON object"])
    problems: list[str] = []
    if data.get("kind") != FINDING_KIND:
        problems.append(f"kind must be {FINDING_KIND!r}")
    if data.get("schema_version") != FINDING_SCHEMA_VERSION:
        problems.append(f"schema_version must be {FINDING_SCHEMA_VERSION}")
    for key in ("title", "summary", "repository"):
        if not _text(data.get(key)):
            problems.append(f"{key} is required")
    commit = data.get("base_commit")
    if commit not in (None, "") and not (
        isinstance(commit, str) and _COMMIT.fullmatch(commit.lower())
    ):
        problems.append("base_commit must be a full hexadecimal commit ID")
    reproducer = data.get("reproducer")
    if not isinstance(reproducer, dict):
        problems.append("reproducer must be an object")
    else:
        command = reproducer.get("command") or []
        if not isinstance(command, list) or not all(isinstance(p, str) for p in command):
            problems.append("reproducer.command must be a list of strings")
            command = []
        if not command and not _text(reproducer.get("script")):
            problems.append("reproducer needs a command or a script")
        if not _text(reproducer.get("expected")):
            problems.append(
                "reproducer.expected must say what output shows the defect"
            )
    conditions = data.get("conditions")
    if not isinstance(conditions, list):
        problems.append("conditions must be a list, empty when the defect needs none")
        conditions = []
    seen: set[str] = set()
    for index, condition in enumerate(conditions, start=1):
        label = f"condition {index}"
        if not isinstance(condition, dict):
            problems.append(f"{label} must be an object")
            continue
        name = condition.get("name")
        if not _text(name):
            problems.append(f"{label} needs a name")
        elif name in seen:
            problems.append(f"{label} repeats the name {name!r}")
        else:
            seen.add(name)
            label = f"condition {name!r}"
        if not isinstance(condition.get("required"), bool):
            problems.append(f"{label} required must be true or false")
        satisfies = condition.get("host_satisfies")
        if not (isinstance(satisfies, bool) or satisfies == "unknown"):
            problems.append(f"{label} host_satisfies must be true, false or \"unknown\"")
        if not _text(condition.get("how_checked")):
            problems.append(f"{label} must say how the host was checked")
        modelled = condition.get("modelled_by")
        if modelled is not None and not isinstance(modelled, str):
            problems.append(f"{label} modelled_by must be text")
    evidence = data.get("evidence", {})
    if not isinstance(evidence, dict) or not all(
        isinstance(value, str) for value in evidence.values()
    ):
        problems.append("evidence must map names to paths or URLs")
    if problems:
        raise FindingError(problems)
    return data


def load_finding(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise FindingError([f"{path} is not valid JSON: {error}"]) from error
    return validate_finding(data)


def write_finding(path: Path, record: dict[str, Any]) -> Path:
    """Validate and write a finding record atomically."""
    validate_finding(record)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def unmet_conditions(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Required conditions this host does not supply or could not confirm."""
    return [
        condition
        for condition in record.get("conditions", [])
        if condition.get("required") and condition.get("host_satisfies") is not True
    ]


def _satisfies(value: Any) -> str:
    if value is True:
        return "yes"
    return "no" if value is False else "unknown"


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def render_finding_markdown(record: dict[str, Any]) -> str:
    """The finding as defect-report prose, conditions table included."""
    reproducer = record["reproducer"]
    lines = [record["summary"].strip(), ""]
    lines += [f"- Repository: {record['repository']}"]
    if record.get("base_commit"):
        lines.append(f"- Base commit: {record['base_commit']}")
    lines += ["", "### Reproducer", ""]
    if reproducer.get("command"):
        lines += ["```", " ".join(reproducer["command"]), "```", ""]
    if _text(reproducer.get("script")):
        lines += ["```python", reproducer["script"].rstrip(), "```", ""]
    lines += [f"Expected evidence of the defect: {reproducer['expected'].strip()}", ""]
    conditions = record.get("conditions", [])
    lines += ["### Conditions the defect needs", ""]
    if not conditions:
        lines += ["None recorded: the defect needs no special host condition.", ""]
    else:
        lines += [
            "| Condition | Required | This host supplies it | How checked | Modelled by |",
            "| --- | --- | --- | --- | --- |",
        ]
        for condition in conditions:
            lines.append(
                f"| {_cell(condition['name'])} "
                f"| {'yes' if condition['required'] else 'no'} "
                f"| {_satisfies(condition['host_satisfies'])} "
                f"| {_cell(condition['how_checked'])} "
                f"| {_cell(condition.get('modelled_by')) or '-'} |"
            )
        lines.append("")
        unmet = unmet_conditions(record)
        if unmet:
            names = ", ".join(condition["name"] for condition in unmet)
            lines += [
                f"This host does not supply every required condition ({names}).",
                "A reproduction here models what the host lacks, and an upstream",
                "report must say so rather than claim a native reproduction.",
                "",
            ]
    evidence = record.get("evidence") or {}
    if evidence:
        lines += ["### Evidence", ""]
        lines += [f"- {name}: {value}" for name, value in evidence.items()]
        lines.append("")
    return "\n".join(lines)
