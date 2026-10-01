"""Draft an editable Python environment plan from declared dependencies."""
from __future__ import annotations

import json
import re
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

#: pip always builds a direct reference (`name @ git+https://...`) from source,
#: and `--no-build-isolation` gives that build only what the environment holds.
#: spikeinterface's `probeinterface @ git+...` failed with `Cannot import
#: 'hatchling.build'`. These are pure Python; an unused one costs nothing.
#: https://github.com/wolfgang-aura/Mailman/issues/364
DIRECT_REFERENCE_BACKENDS = ("hatchling", "hatch-vcs", "setuptools-scm", "flit-core", "poetry-core")


def _declares_direct_reference(metadata: dict, groups: dict) -> bool:
    declared = [*metadata.get("dependencies", []),
                *(entry for entries in metadata.get("optional-dependencies", {}).values() for entry in entries),
                *(entry for entries in groups.values() for entry in entries)]
    return any(isinstance(entry, str) and re.match(r"^[A-Za-z0-9._\[\], -]+@", entry) for entry in declared)


def _builds_compiled_extensions(workspace: Path) -> bool:
    """Whether the target's setup.py compiles extension modules."""
    setup = workspace / "setup.py"
    if not setup.is_file():
        return False
    text = setup.read_text(encoding="utf-8", errors="replace")
    return "Extension(" in text or "ext_modules" in text


#: Copies the installed release's compiled modules into the workspace (the
#: step's working directory) at the same relative paths. Targets gitignore
#: them, so the tree stays clean. Mailman #213.
_COPY_COMPILED = (
    "import importlib.metadata as m,os,shutil,sys\n"
    "d=m.distribution(sys.argv[1]);n=0\n"
    "for f in d.files or []:\n"
    " if f.suffix in ('.pyd','.so'):\n"
    "  dst=os.path.join(os.getcwd(),str(f));os.makedirs(os.path.dirname(dst),exist_ok=True)\n"
    "  shutil.copy2(f.locate(),dst);n+=1\n"
    "print('copied',n,'compiled module(s)');sys.exit(0 if n else 1)"
)
_WORKSPACE_ON_PATH = (
    "import os,sys,sysconfig,pathlib\n"
    "pathlib.Path(sysconfig.get_paths()['purelib'],sys.argv[1]+'-workspace.pth').write_text(os.getcwd())"
)


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


def _poe_test_extras(project: dict, declared: dict) -> list[str]:
    """Extras the poe `test` task asks uv for.

    schwifty runs `uv run --extra pydantic pytest`, so a test imports pydantic
    although no test extra names it. Mailman #360.
    """
    task = project.get("tool", {}).get("poe", {}).get("tasks", {}).get("test")
    if isinstance(task, dict):
        task = task.get("cmd") or task.get("shell")
    if not isinstance(task, str):
        return []
    if "--all-extras" in task:
        return list(declared)
    return [name for name in re.findall(r"--extra[= ](\S+)", task) if name in declared]


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
    extra = ",".join(dict.fromkeys([*([extra] if extra else []), *_poe_test_extras(project, extras)])) or None
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
    if _declares_direct_reference(metadata, groups):
        dependencies = list(dict.fromkeys([*dependencies, *DIRECT_REFERENCE_BACKENDS]))
    if "--no-build-isolation" in install and "-e" in install and _builds_editables_by_import(project):
        dependencies = list(dict.fromkeys([*dependencies, "editables"]))
    review = "Read CI and contributing instructions before execution. This draft does not reproduce uv or poetry lock resolution. Adjust the interpreter to requires-python and the supported CI matrix."
    # No compiler on the Windows host, so an editable build of C extensions
    # cannot succeed. Borrow the release wheel's compiled modules and run the
    # workspace's Python source over them. Mailman #213.
    compiled = sys.platform == "win32" and _builds_compiled_extensions(workspace)
    name = metadata.get("name")
    if compiled and isinstance(name, str):
        runtime = [entry for entry in metadata.get("dependencies", []) if isinstance(entry, str)]
        dependencies = list(dict.fromkeys([*dependencies, *runtime, "pytest"]))
        install_steps = [
            {"name": "install-release-wheel-for-compiled-modules", "command": [interpreter, "-m", "pip", "install", *constraints, "--only-binary", ":all:", "--no-deps", name]},
            {"name": "copy-compiled-modules-into-workspace", "command": [interpreter, "-c", _COPY_COMPILED, name]},
            {"name": "remove-release-wheel", "command": [interpreter, "-m", "pip", "uninstall", "-y", name]},
            {"name": "workspace-on-path", "command": [interpreter, "-c", _WORKSPACE_ON_PATH, name]},
        ]
        review += (
            " setup.py builds compiled extensions and this host has no compiler: the compiled"
            " modules come from the latest release wheel, not the base commit. A change to C"
            " source cannot be tested here; pick another target for that."
        )
    else:
        compiled = False
        install_steps = [{"name": "install-target", "command": install}]
    plan = {
        "schema_version": 1,
        "steps": [
            {"name": "create-environment", "command": [python, "-m", "venv", "{environment}"], "working_directory": "run"},
            {"name": "install-build-and-test-dependencies", "command": [interpreter, "-m", "pip", "install", BINARY_POLICY, *constraints, *dependencies]},
            *install_steps,
        ],
        "register": [{"name": "python", "executable": interpreter}],
        "draft": {
            "source": str(source.resolve()), "requires_python": metadata.get("requires-python"),
            "extra": extra, "group": group, "hatch_environments": hatch_environments,
            "compiled_extensions": compiled,
            "review": review,
        },
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    if constraints:
        (destination.parent / HOST_CONSTRAINTS_FILENAME).write_text(
            "\n".join(HOST_BLOCKED_RELEASES) + "\n", encoding="utf-8"
        )
    destination.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    return plan
