from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from mailman.executor import execute
from mailman.toolchain import probe_tool
from mailman.workspace import inspect_workspace, workspace_fingerprint


ENVIRONMENT_DIRECTORY = "environment"

#: The only programs a preparation step may start. A plan is drafted from a
#: target's own contributing guide, so its contents are as trustworthy as the
#: target, and a step is the first thing Mailman runs on the host. Restricting
#: the executable stops a plan that reaches for `curl`, a shell, or a package
#: manager installing system-wide.
#:
#: This does not make a hostile target safe. `pip install -e .` still runs the
#: target's build back end, and it has to. The provenance gate in
#: `mailman.screen` is what decides whether that target is worth trusting with
#: it. See https://github.com/wolfgang-aura/Mailman/issues/48.
PERMITTED_EXECUTABLES = ("git",)


def _executable_name(command: str) -> str:
    """The bare program name, whichever separator the plan happened to use."""
    return command.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _is_python(name: str) -> bool:
    stem = name[:-4] if name.endswith(".exe") else name
    return stem == "python" or (
        stem.startswith("python") and stem[6:].replace(".", "").isdigit()
    )


def check_executable(command: Sequence[str], *, step: str) -> None:
    """Refuse a step that starts anything but a Python interpreter or git."""
    name = _executable_name(command[0])
    if _is_python(name) or name in PERMITTED_EXECUTABLES:
        return
    raise ValueError(
        f"step {step} runs {command[0]!r}. A preparation step may run a Python "
        f"interpreter or {', '.join(PERMITTED_EXECUTABLES)}, nothing else."
    )


def _substitute(value: str, replacements: dict[str, str]) -> str:
    result = value
    for token, replacement in replacements.items():
        result = result.replace(token, replacement)
    return result


def load_plan(path: Path) -> dict[str, Any]:
    """Read and validate a dependency preparation plan."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("environment plan must be an object with schema_version 1")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("environment plan must list at least one step")
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"step {index} must be an object")
        name = step.get("name")
        command = step.get("command")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"step {index} must have a name")
        if not isinstance(command, list) or not command:
            raise ValueError(f"step {name} must have a non-empty command list")
        if not all(isinstance(part, str) for part in command):
            raise ValueError(f"step {name} command must be a list of strings")
        location = step.get("working_directory", "workspace")
        if location not in ("workspace", "environment", "run"):
            raise ValueError(
                f"step {name} working_directory must be workspace, environment, or run"
            )
        check_executable(command, step=name)
    registrations = data.get("register", [])
    if not isinstance(registrations, list):
        raise ValueError("environment plan register must be a list")
    for entry in registrations:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not isinstance(entry.get("executable"), str)
        ):
            raise ValueError("each register entry needs a name and an executable")
    return data


def _write_record(run_directory: Path, record: dict[str, Any]) -> Path:
    destination = run_directory / "environment.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def prepare_environment(
    run_directory: Path,
    *,
    workspace: Path,
    plan: dict[str, Any],
    timeout_seconds: float = 1800,
    announce: Callable[[str], None] = lambda message: None,
) -> dict[str, Any]:
    """Install a target repository's dependencies outside its working tree.

    Every step runs without a shell and is recorded. The environment lives in the
    run directory so that the workspace stays clean for the primary agent, and a
    step that dirties the workspace fails preparation instead of surfacing later
    as a refused orchestration.

    What preparation may not do is change the workspace. Whether the workspace
    was already dirty is a separate question, and one the primary role asks for
    itself, so a run whose workspace holds its own candidate can still rebuild
    its environment.
    """
    workspace_path = workspace.resolve(strict=True)
    before = inspect_workspace(workspace_path)
    fingerprint_before = workspace_fingerprint(workspace_path)
    environment_path = (run_directory / ENVIRONMENT_DIRECTORY).resolve()
    environment_path.mkdir(parents=True, exist_ok=True)
    replacements = {
        "{environment}": str(environment_path),
        "{workspace}": str(workspace_path),
        "{run}": str(run_directory.resolve()),
    }
    locations = {
        "workspace": workspace_path,
        "environment": environment_path,
        "run": run_directory.resolve(),
    }

    record: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "environment_path": str(environment_path),
        "workspace_path": str(workspace_path),
        "plan": plan,
        "steps": [],
        "registered": [],
        "workspace_clean": None,
        "workspace_changes_before": list(before.changes),
        "workspace_unchanged": None,
        "success": False,
    }

    for step in plan["steps"]:
        command = [_substitute(part, replacements) for part in step["command"]]
        # Checked again after substitution, because load_plan saw the token and
        # this sees the path it expanded to. A plan built in memory by a caller
        # never passed through load_plan at all.
        check_executable(command, step=step["name"])
        location = locations[step.get("working_directory", "workspace")]
        announce(f"run  environment:{step['name']}: {' '.join(command)}")
        result = execute(
            command, working_directory=location, timeout_seconds=timeout_seconds
        )
        ok = not result.timed_out and result.exit_code == 0
        record["steps"].append(
            {"name": step["name"], "ok": ok, "command": result.to_dict()}
        )
        announce(
            f"{'ok  ' if ok else 'stop'} environment:{step['name']}: "
            f"exit code {result.exit_code}"
        )
        if not ok:
            record["detail"] = f"step {step['name']} failed"
            record["finished_at"] = datetime.now(UTC).isoformat()
            _write_record(run_directory, record)
            return record

    state = inspect_workspace(workspace_path)
    record["workspace_clean"] = state.clean
    added = [change for change in state.changes if change not in before.changes]
    unchanged = workspace_fingerprint(workspace_path) == fingerprint_before
    record["workspace_changes_after"] = list(state.changes)
    record["workspace_unchanged"] = unchanged
    if not unchanged:
        record["added_paths"] = added or list(state.changes)
        record["detail"] = (
            "preparation left changes in the workspace. Install into "
            "{environment} instead, so the primary agent starts from a clean "
            "checkout at the base commit."
        )
        record["finished_at"] = datetime.now(UTC).isoformat()
        _write_record(run_directory, record)
        return record

    for entry in plan.get("register", []):
        executable = Path(_substitute(entry["executable"], replacements))
        probe_arguments = entry.get("probe_arguments") or ["--version"]
        announce(f"run  environment:register {entry['name']}: {executable}")
        probe = probe_tool(
            run_directory,
            name=entry["name"],
            executable=executable,
            probe_arguments=list(probe_arguments),
            timeout_seconds=min(timeout_seconds, 120),
        )
        registered = not probe.timed_out and probe.exit_code == 0
        record["registered"].append(
            {
                "name": entry["name"],
                "executable": str(executable),
                "registered": registered,
                "probe": probe.to_dict(),
            }
        )
        if not registered:
            record["detail"] = f"could not register {entry['name']} for the run"
            record["finished_at"] = datetime.now(UTC).isoformat()
            _write_record(run_directory, record)
            return record

    record["success"] = True
    record["finished_at"] = datetime.now(UTC).isoformat()
    _write_record(run_directory, record)
    return record


def load_environment_record(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / "environment.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def environment_command(
    run_directory: Path, command: Sequence[str]
) -> list[str]:
    """Expand `{environment}` in a command a caller passes on the command line."""
    environment_path = str((run_directory / ENVIRONMENT_DIRECTORY).resolve())
    return [part.replace("{environment}", environment_path) for part in command]
