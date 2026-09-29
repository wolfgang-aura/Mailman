"""Draft an editable Python environment plan from declared dependencies."""
from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


def _builds_editables_by_import(project: dict) -> bool:
    """Whether an editable build of this target imports the `editables` package.

    hatchling's editable hook imports `editables`, and `--no-build-isolation`
    means pip will not fetch it: the install fails with `ModuleNotFoundError`
    unless the build-dependency step already carries it. An undeclared backend
    counts, because PEP 517 lets the target grow one without telling us here
    and an unused pure-Python dependency costs nothing.
    """
    backend = project.get("build-system", {}).get("build-backend")
    return not isinstance(backend, str) or backend.split(".")[0] == "hatchling"


#: Hatch environments a target runs its tests in. `default` is where `hatch run
#: test` looks, and edgartools declares pytest-asyncio, pytest-env and vcrpy
#: only there: a plan without them failed 58 tests at base and candidate alike.
#: https://github.com/wolfgang-aura/Mailman/issues/133
HATCH_TEST_ENVIRONMENTS = ("default", "test", "tests", "hatch-test")

#: Prefer a wheel, and fall back to an older wheel before a newer sdist, but
#: still install a package that publishes no wheel at all. `--only-binary=:all:`
#: refused beets' `langdetect` and `titlecase`, which are pure Python and build
#: without a compiler. A package that needs one fails either way.
BINARY_POLICY = "--prefer-binary"

#: Releases whose compiled modules Windows Application Control blocks on this
#: host at import time, after pip installs them cleanly. Written as pip
#: constraints, so pip picks the newest allowed release inside the target's own
#: range and installs nothing a target does not ask for. scikit-learn 1.9.1
#: blocked 12 `.pyd` modules on nilearn#6607 while 1.8.0 imported; pandas 3.0.6
#: cp314 is blocked while 3.0.5 is not. Mailman #146. pyproj 3.8.0 cp314
#: blocked `_network` on PyPSA#1938 while 3.7.2 imported. Mailman #197.
HOST_BLOCKED_RELEASES = (
    "scikit-learn!=1.9.1",
    "pandas!=3.0.6",
    "pyproj!=3.8.0",
)
HOST_CONSTRAINTS_FILENAME = "host-constraints.txt"


def _hatch_test_dependencies(project: dict) -> tuple[list[str], list[str]]:
    """Requirements declared in the hatch environments tests run in.

    An entry with hatch context formatting (`{root:uri}`, `{env:...}`) names a
    path or value only hatch can resolve, so it is left out rather than handed
    to pip as a literal.
    """
    environments = project.get("tool", {}).get("hatch", {}).get("envs", {})
    dependencies: list[str] = []
    used: list[str] = []
    for name in HATCH_TEST_ENVIRONMENTS:
        environment = environments.get(name) if isinstance(environments, dict) else None
        if not isinstance(environment, dict):
            continue
        declared = [
            entry
            for key in ("dependencies", "extra-dependencies")
            for entry in environment.get(key, [])
            if isinstance(entry, str) and "{" not in entry
        ]
        if declared:
            used.append(name)
            dependencies.extend(declared)
    return dependencies, used


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
    constraints: list[str] = []
    if sys.platform == "win32":
        constraint_file = destination.parent / HOST_CONSTRAINTS_FILENAME
        constraints = ["-c", str(constraint_file.resolve())]
    build = project.get("build-system", {}).get("requires", ["setuptools"])
    hatch, hatch_environments = _hatch_test_dependencies(project)
    dependencies = list(dict.fromkeys([*build, *(expand(group) if group else []), *hatch]))
    install = [interpreter, "-m", "pip", "install", BINARY_POLICY, *constraints, "--no-build-isolation", "-e", f".[{extra}]" if extra else "."]
    # Without build isolation a dependency that ships only an sdist builds with
    # whatever the environment holds, and a fresh venv holds no setuptools.
    dependencies = list(dict.fromkeys([*dependencies, "setuptools"]))
    if "--no-build-isolation" in install and "-e" in install and _builds_editables_by_import(project):
        dependencies = list(dict.fromkeys([*dependencies, "editables"]))
    plan = {
        "schema_version": 1,
        "steps": [
            {"name": "create-environment", "command": [python, "-m", "venv", "{environment}"], "working_directory": "run"},
            {"name": "install-build-and-test-dependencies", "command": [interpreter, "-m", "pip", "install", BINARY_POLICY, *constraints, *dependencies]},
            {"name": "install-target", "command": install},
        ],
        "register": [{"name": "python", "executable": interpreter}],
        "draft": {
            "source": str(source.resolve()), "requires_python": metadata.get("requires-python"),
            "extra": extra, "group": group, "hatch_environments": hatch_environments,
            "review": "Read CI and contributing instructions before execution. This draft does not reproduce uv or poetry lock resolution. Adjust the interpreter to requires-python and the supported CI matrix.",
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    if constraints:
        (destination.parent / HOST_CONSTRAINTS_FILENAME).write_text(
            "\n".join(HOST_BLOCKED_RELEASES) + "\n", encoding="utf-8"
        )
    destination.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan
