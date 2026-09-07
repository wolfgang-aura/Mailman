"""Draft an editable Python environment plan from declared dependencies."""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


def draft_plan(workspace: Path, destination: Path, *, python: str = sys.executable) -> dict:
    if destination.exists():
        raise ValueError(f"plan already exists at {destination}; edit it instead of overwriting")
    source = workspace / "pyproject.toml"
    if not source.is_file():
        raise ValueError("no pyproject.toml; derive a plan from the target's setup instructions")
    project = tomllib.loads(source.read_text(encoding="utf-8"))
    metadata = project.get("project", {})
    extras = metadata.get("optional-dependencies", {})
    groups = project.get("dependency-groups", {})
    names = ("test", "tests", "testing", "dev")
    extra = next((name for name in names if name in extras), None)
    group = next((name for name in names if name in groups), None)

    def expand(name: str, visiting: tuple[str, ...] = ()) -> list[str]:
        if name in visiting or name not in groups:
            raise ValueError(f"invalid dependency group reference: {name}")
        dependencies = []
        for entry in groups[name]:
            if isinstance(entry, str):
                dependencies.append(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("include-group"), str):
                dependencies.extend(expand(entry["include-group"], (*visiting, name)))
            else:
                raise ValueError(f"unsupported dependency declaration in group {name}")  # noqa: TRY004 -- invalid file content
        return dependencies

    interpreter = "{environment}/Scripts/python.exe" if sys.platform == "win32" else "{environment}/bin/python"
    build = project.get("build-system", {}).get("requires", ["setuptools"])
    dependencies = list(dict.fromkeys([*build, *(expand(group) if group else [])]))
    plan = {
        "schema_version": 1,
        "steps": [
            {"name": "create-environment", "command": [python, "-m", "venv", "{environment}"], "working_directory": "run"},
            {"name": "install-build-and-test-dependencies", "command": [interpreter, "-m", "pip", "install", "--only-binary=:all:", *dependencies]},
            {"name": "install-target", "command": [interpreter, "-m", "pip", "install", "--only-binary=:all:", "--no-build-isolation", "-e", f".[{extra}]" if extra else "."]},
        ],
        "register": [{"name": "python", "executable": interpreter}],
        "draft": {
            "source": str(source.resolve()), "requires_python": metadata.get("requires-python"),
            "extra": extra, "group": group,
            "review": "Read CI and contributing instructions before execution. This draft does not reproduce uv or poetry lock resolution. Adjust the interpreter to requires-python and the supported CI matrix.",
        },
    }
    if not dependencies:
        plan["steps"].pop(1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan
