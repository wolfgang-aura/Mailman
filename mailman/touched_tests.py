"""Run every test file that exercises a module the diff touches.

edgartools#1329 failed CI on `tests/xbrl/test_statement_drilldown.py`, a file
the primary never ran. It imports `edgar.xbrl.xbrl`, the module the diff
changed, and would have failed locally in under a second. The primary's
focused check and Mailman's recorded verification command both covered only
the tests the primary chose. This stage chooses by the diff instead: every
test file whose text imports or names a touched module runs in the run's own
environment before `handoff-check` can pass.
See https://github.com/wolfgang-aura/Mailman/issues/115.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from mailman.environment import ENVIRONMENT_DIRECTORY
from mailman.executor import CommandResult, execute
from mailman.toolchain import toolchain_executable

TOUCHED_TESTS_FILENAME = "touched-tests.json"
TOUCHED_TESTS_SCHEMA_VERSION = 1
#: Digest of this module. A failure recorded by other code is run again, so
#: a fix here reaches a run that already holds a record for the same diff.
#: Mailman #216.
TOUCHED_TESTS_CODE_VERSION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
#: More than this and the stage is no longer a focused check; the record says
#: which files were left out so the reader can widen it by hand.
TOUCHED_TESTS_CAP = 25
TOUCHED_TESTS_TIMEOUT_SECONDS = 20 * 60
PROBE_TIMEOUT_SECONDS = 120

#: Layout prefixes that are not part of the import path.
_LAYOUT_PREFIXES = ("src", "lib", "python")
_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "build",
        "dist",
        ".eggs",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ENVIRONMENT_DIRECTORY,
    }
)
#: Nothing-to-run outcomes. The stage ran and has nothing to report on, which
#: is not the same as never having run.
# Source a test can reach; `.pyi` stubs are what stub packages ship.
_SOURCE_SUFFIXES = (".py", ".pyi")
_NOTHING_TO_RUN = frozenset({"no-source-change", "no-matching-tests"})


def diff_sha256(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def _is_test_path(path: str) -> bool:
    from mailman.submission import _is_test_path as is_test_path

    return is_test_path(path)


def pytest_import_roots(workspace: Path) -> tuple[str, ...]:
    """The directories pytest's `pythonpath` option puts on `sys.path`.

    spack sets `pythonpath = lib/spack`, so `lib/spack/spack/package_base.py`
    is imported as `spack.package_base`. Mailman #293.
    """
    import configparser
    import tomllib

    roots: list[str] = []
    for name, section in (
        ("pytest.ini", "pytest"),
        ("tox.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
    ):
        path = workspace / name
        if not path.is_file():
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(path.read_text(encoding="utf-8", errors="replace"))
        except configparser.Error:
            continue
        roots.extend(parser.get(section, "pythonpath", fallback="").split())
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            options = (
                tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
                .get("tool", {}).get("pytest", {}).get("ini_options", {})
            )
        except tomllib.TOMLDecodeError:
            options = {}
        value = options.get("pythonpath") if isinstance(options, dict) else None
        roots.extend(value.split() if isinstance(value, str) else value or [])
    cleaned = {
        str(root).replace("\\", "/").strip("/").removeprefix("./")
        for root in roots
        if isinstance(root, str)
    }
    return tuple(sorted((root for root in cleaned if root and root != "."), key=len, reverse=True))


def module_names(path: str, roots: tuple[str, ...] = ()) -> list[str]:
    """The names a test could import or mention to reach this file.

    `edgar/xbrl/xbrl.py` gives `edgar.xbrl.xbrl`, its package `edgar.xbrl`,
    and the bare stem `xbrl`. The top-level package alone is left out: every
    test in the repository imports it, which is a full run, not a focused one.
    A pytest `pythonpath` root in `roots` is not part of the import path.
    """
    normalized = path.replace("\\", "/")
    if not normalized.endswith(_SOURCE_SUFFIXES):
        return []
    for root in roots:
        if normalized.startswith(root + "/"):
            normalized = normalized[len(root) + 1:]
            break
    segments = normalized.rsplit(".", 1)[0].split("/")
    while len(segments) > 1 and segments[0] in _LAYOUT_PREFIXES:
        segments = segments[1:]
    # PEP 561: a `pandas-stubs` directory provides `pandas`. Mailman #307.
    if len(segments) > 1 and segments[0].endswith("-stubs"):
        segments[0] = segments[0][: -len("-stubs")]
    if segments[-1] == "__init__":
        segments = segments[:-1]
    if not segments or not all(re.fullmatch(r"\w+", segment) for segment in segments):
        return []
    names: list[str] = []
    dotted = ".".join(segments)
    names.append(dotted)
    # The module's own package, never its grandparents: for
    # `nicegui/elements/leaflet/leaflet.py`, `nicegui.elements` is imported by
    # most of the test suite, and matching it ran eight unrelated files and
    # not the one named after the module.
    if len(segments) > 2:
        names.append(".".join(segments[:-1]))
    stem = segments[-1]
    # A stem that names a stdlib module matches every test importing that
    # module: `collections.py` pulled in 19 unrelated files. Mailman #248.
    if stem not in names and (len(segments) == 1 or stem not in sys.stdlib_module_names):
        names.append(stem)
    return names


_OPTIONAL_LAYOUT = "(?:(?:" + "|".join(_LAYOUT_PREFIXES) + r")\.)?"


def _reference_pattern(name: str) -> re.Pattern[str]:
    escaped = _OPTIONAL_LAYOUT + re.escape(name)
    if "." in name:
        return re.compile(
            rf"(?m)^\s*(?:from\s+{escaped}\b|import\s+{escaped}\b)"
            rf"|[\"']{escaped}(?:\.[\w.]*)?[\"']"
        )
    # A bare stem: `import x`, `from x import`, or `from pkg import ..., x`.
    return re.compile(
        rf"(?m)^\s*(?:from\s+{escaped}\b|import\s+{escaped}\b"
        rf"|from\s+[\w.]+\s+import\b[^\n]*\b{re.escape(name)}\b)"
    )


def _is_test_module(relative: str, python_files: list[str] | None = None) -> bool:
    """A file pytest would collect on its own, not a helper beside the tests.

    `src/tests/testdummy/signals.py` lives under `tests/` and is a helper
    package pytest never collects; naming it on the command line collected
    nothing and the stage exited 5 and called the candidate unverified.
    """
    name = relative.replace("\\", "/").rsplit("/", 1)[-1].lower()
    # pytest matches `python_files` only among .py files; uproot5#1529's
    # tests/samples/test_1529.root was handed to pytest, which exited 4 (#376).
    if not name.endswith(".py"):
        return False
    if python_files:
        from fnmatch import fnmatch

        return any(fnmatch(name, pattern.lower()) for pattern in python_files)
    return name.startswith("test_") or name.endswith(("_test.py", "_tests.py"))


def _pytest_option(workspace: Path, option: str) -> list[str]:
    """A list-valued pytest option from the first config file that sets it, or `[]`."""
    import configparser
    import tomllib

    for name, section in (
        ("pytest.ini", "pytest"),
        ("pyproject.toml", None),
        ("tox.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
    ):
        path = workspace / name
        if not path.is_file():
            continue
        if section is None:
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
                continue
            options = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
            declared = options.get(option) if isinstance(options, dict) else None
            if isinstance(declared, str):
                declared = declared.split()
            if isinstance(declared, list) and declared:
                return [str(entry) for entry in declared]
            continue
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(path, encoding="utf-8")
        except (OSError, configparser.Error, UnicodeDecodeError):
            continue
        if parser.has_option(section, option):
            entries = parser.get(section, option).split()
            if entries:
                return entries
    return []


def pytest_testpaths(workspace: Path) -> list[str]:
    """The target's pytest `testpaths`, from the first file that sets it, or `[]`.

    nilearn sets `testpaths = ["nilearn"]`; without it, a gallery script
    `examples/.../plot_second_level_association_test.py` was run as a test (#145).
    """
    return [
        entry.replace("\\", "/").strip("/")
        for entry in _pytest_option(workspace, "testpaths")
    ]


def pytest_python_files(workspace: Path) -> list[str]:
    """The target's pytest `python_files` globs, or `[]` for pytest's defaults.

    pylint sets `python_files = ["*test_*.py"]` and names its checker tests
    `unittest_typecheck.py`; the default patterns never selected them (#303).
    """
    return _pytest_option(workspace, "python_files")


def _under(relative: str, roots: list[str]) -> bool:
    return any(
        root in ("", ".") or relative == root or relative.startswith(root + "/")
        for root in roots
    )


def _test_files(workspace: Path) -> list[str]:
    """Every collectable test file in the workspace, as a `/`-separated relative path.

    Limited to the target's pytest `testpaths` when it sets them.
    """
    roots = pytest_testpaths(workspace)
    python_files = pytest_python_files(workspace)
    found: list[str] = []
    for root, directories, files in os.walk(workspace):
        # A `testing` directory inside a package is library code, not a test
        # tree: `nicegui/testing/user_interaction.py` is the User fixture,
        # and collecting it as a test file made the stage exit 2.
        directories[:] = sorted(
            name
            for name in directories
            if name not in _SKIPPED_DIRECTORIES
            and not (name == "testing" and (Path(root) / "__init__.py").is_file())
        )
        relative_root = Path(root).relative_to(workspace)
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            relative = (relative_root / name).as_posix()
            if relative.startswith("./"):
                relative = relative[2:]
            if roots and not _under(relative, roots):
                continue
            if _is_test_path(relative) and _is_test_module(relative, python_files):
                found.append(relative)
    return found


def _directness(name: str, modules: dict[str, list[str]]) -> int:
    """0 for a module's full dotted path, 1 for its package, 2 for a bare stem."""
    return min(
        (names.index(name) for names in modules.values() if name in names), default=2
    )


def _package_only_from_outside(
    relative: str, matched: list[str], modules: dict[str, list[str]]
) -> bool:
    """A file that names only a touched module's package, from outside it.

    nilearn's estimator-check sweep imports `nilearn.glm.first_level` beside
    every other estimator; matching `nilearn.glm` ran 1644 sklearn checks for
    a one-line change in `nilearn/glm/regression.py` and the stage timed out.
    Tests inside the package's own directory still count on the package name.
    """
    for source, names in modules.items():
        if len(names) < 3:
            continue
        package = names[1]
        if any(name != package for name in matched if name in names):
            return False
        if package not in matched:
            continue
        directory = source.replace("\\", "/").rsplit("/", 1)[0]
        if relative.startswith(directory + "/"):
            return False
    return all(
        any(len(names) >= 3 and name == names[1] for names in modules.values())
        for name in matched
    )


def select_test_files(
    workspace: Path, changed_paths: list[str], *, cap: int = TOUCHED_TESTS_CAP
) -> dict[str, Any]:
    """Pick the test files whose text imports or names a touched module."""
    source_files = [
        path
        for path in changed_paths
        if not _is_test_path(path) and path.replace("\\", "/").endswith(_SOURCE_SUFFIXES)
    ]
    roots = pytest_import_roots(workspace)
    python_files = pytest_python_files(workspace)
    modules = {path: module_names(path, roots) for path in source_files}
    patterns = {
        name: _reference_pattern(name)
        for names in modules.values()
        for name in names
    }
    selected: list[dict[str, Any]] = []
    indirect: list[str] = []
    if patterns:
        # A test file the diff itself changes is the primary's own coverage of
        # the change; it runs first whether or not its text names the module.
        # pretix#6518 changed `src/tests/base/test_invoices.py`, which reaches
        # `pretix.base.invoicing.pdf` only through a service function, and the
        # stage left it out.
        changed_tests = [
            path.replace("\\", "/")
            for path in changed_paths
            if _is_test_path(path) and _is_test_module(path, python_files)
            and (workspace / path).is_file()
        ]
        for relative in changed_tests:
            selected.append(
                {"path": relative, "matched": [], "reason": "changed by the diff"}
            )
        matched_by_import: list[dict[str, Any]] = []
        indirect: list[str] = []
        for relative in _test_files(workspace):
            if relative in changed_tests:
                continue
            try:
                text = (workspace / relative).read_text(
                    encoding="utf-8", errors="replace"
                )
            except OSError:
                continue
            matched = [name for name, pattern in patterns.items() if pattern.search(text)]
            if matched and _package_only_from_outside(relative, matched, modules):
                indirect.append(relative)
                continue
            if matched:
                matched_by_import.append(
                    {
                        "path": relative,
                        "matched": matched,
                        "reason": "imports or names " + ", ".join(matched),
                    }
                )
        # Before the cap: a file importing the module itself beats one that
        # only names its package or bare stem. Alphabetical capping dropped
        # the file that failed CI on edgartools#1329 (#127).
        matched_by_import.sort(
            key=lambda entry: min(_directness(name, modules) for name in entry["matched"])
        )
        selected.extend(matched_by_import)
    capped = len(selected) > cap
    omitted = [entry["path"] for entry in selected[cap:]] if capped else []
    return {
        "source_files": source_files,
        "modules": modules,
        "candidates": len(selected),
        "selected": selected[:cap],
        "capped": capped,
        "cap": cap,
        "omitted": omitted,
        "indirect": indirect,
    }


_NETWORK_MARKER = re.compile(r"^\s*network\s*(?::|$)")
_CONFTEST_NETWORK_MARKER = re.compile(
    r"addinivalue_line\(\s*[\"']markers[\"']\s*,\s*[\"']network\b"
)


def _ini_markers(path: Path, section: str) -> list[str]:
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error, UnicodeDecodeError):
        return []
    if not parser.has_option(section, "markers"):
        return []
    return parser.get(section, "markers").splitlines()


def network_marker_registered(workspace: Path) -> bool:
    """Whether the target registers a pytest `network` marker.

    edgartools marks SEC-bound tests `network` and CI deselects them; run here
    they failed with IdentityNotSetError and blocked handoff-check (#127).
    """
    import tomllib

    markers: list[str] = []
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            data = {}
        options = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
        declared = options.get("markers") if isinstance(options, dict) else None
        if isinstance(declared, list):
            markers.extend(str(marker) for marker in declared)
        elif isinstance(declared, str):
            markers.extend(declared.splitlines())
    for name, section in (
        ("pytest.ini", "pytest"),
        ("tox.ini", "pytest"),
        ("setup.cfg", "tool:pytest"),
    ):
        path = workspace / name
        if path.is_file():
            markers.extend(_ini_markers(path, section))
    if any(_NETWORK_MARKER.match(marker) for marker in markers):
        return True
    for conftest in (workspace / "conftest.py", workspace / "tests" / "conftest.py"):
        try:
            text = conftest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _CONFTEST_NETWORK_MARKER.search(text):
            return True
    return False


def environment_python(run_directory: Path) -> str | None:
    """The interpreter `prepare-environment` registered, or the venv's own."""
    try:
        pinned = toolchain_executable(run_directory, "python")
    except (OSError, ValueError):
        pinned = None
    if pinned:
        return pinned
    environment = run_directory / ENVIRONMENT_DIRECTORY
    for candidate in (
        environment / "Scripts" / "python.exe",
        environment / "bin" / "python",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


_PYTEST_COUNTS = {
    "passed": re.compile(r"(\d+) passed"),
    "failed": re.compile(r"(\d+) failed"),
    "errors": re.compile(r"(\d+) errors?\b"),
    "skipped": re.compile(r"(\d+) skipped"),
}
_UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
_UNITTEST_FAILURES = re.compile(r"failures=(\d+)")
_UNITTEST_ERRORS = re.compile(r"errors=(\d+)")
_UNITTEST_SKIPPED = re.compile(r"skipped=(\d+)")


def parse_counts(runner: str, stdout: str, stderr: str) -> dict[str, int | None]:
    """Read passed/failed/errors/skipped out of a runner's summary line."""
    counts: dict[str, int | None] = {
        "passed": None,
        "failed": None,
        "errors": None,
        "skipped": None,
    }
    if runner == "pytest":
        summary = None
        for line in reversed((stdout + "\n" + stderr).splitlines()):
            if re.search(r"\d+ (passed|failed|error)", line):
                summary = line
                break
        if summary is None:
            return counts
        for key, pattern in _PYTEST_COUNTS.items():
            found = pattern.search(summary)
            counts[key] = int(found.group(1)) if found else 0
        return counts
    text = stdout + "\n" + stderr
    ran = _UNITTEST_RAN.search(text)
    if ran is None:
        return counts
    failures = _UNITTEST_FAILURES.search(text)
    errors = _UNITTEST_ERRORS.search(text)
    skipped = _UNITTEST_SKIPPED.search(text)
    counts["failed"] = int(failures.group(1)) if failures else 0
    counts["errors"] = int(errors.group(1)) if errors else 0
    counts["skipped"] = int(skipped.group(1)) if skipped else 0
    counts["passed"] = (
        int(ran.group(1)) - counts["failed"] - counts["errors"] - counts["skipped"]
    )
    return counts


def _tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.splitlines()[-lines:])


_COLLECTING = re.compile(r"(?m)^_{3,} ERROR collecting (.+?) _{3,}\s*$")
_SECTION_END = re.compile(r"(?m)^(?:_{3,} |={3,})")
_NO_MODULE = re.compile(r"(?:ModuleNotFoundError|ImportError): No module named '([\w.]+)'")
#: A compiled module Windows Application Control refuses to load on this host.
#: The block is on a binary the diff did not build. Mailman #214.
_HOST_BLOCKED_DLL = re.compile(
    r"DLL load failed while importing ([\w.]+): An Application Control policy "
    r"has blocked this file"
)


def _shared_directory(files: list[str]) -> str | None:
    """The one directory below the root that holds every file, if there is one."""
    parents = {PurePosixPath(path.replace("\\", "/")).parent.as_posix() for path in files}
    if len(parents) != 1:
        return None
    parent = parents.pop()
    return None if parent in (".", "") else parent


def _unsuccessful(counts: dict[str, int | None]) -> float:
    if counts.get("failed") is None and counts.get("errors") is None:
        return float("inf")
    return (counts.get("failed") or 0) + (counts.get("errors") or 0)


def _is_local_module(workspace: Path, name: str) -> bool:
    top = name.split(".", 1)[0]
    for prefix in ("", *_LAYOUT_PREFIXES):
        base = workspace / prefix if prefix else workspace
        if (base / top).is_dir() or (base / f"{top}.py").is_file():
            return True
    return False


def missing_extra_collection_errors(
    output: str, workspace: Path, files: list[str]
) -> dict[str, str]:
    """`{test file: reason}` for collection errors the host, not the diff, caused.

    A test file that imports an optional dependency the run environment does
    not have fails at collection, and pytest stops the whole run with exit 2.
    Only `No module named 'x'` for a module that is not part of the workspace
    counts: `cannot import name` from the target's own package may be the diff's
    fault and still fails the stage (#127). A DLL this host's Application Control
    blocks counts too, with its own reason (#214).

    Returns `{test file: reason it was left out}`.
    """
    wanted = {path.replace("\\", "/"): path for path in files}
    missing: dict[str, str] = {}
    for header in _COLLECTING.finditer(output):
        path = header.group(1).strip().replace("\\", "/")
        if path not in wanted:
            continue
        end = _SECTION_END.search(output, header.end())
        section = output[header.end() : end.start() if end else len(output)]
        found = _NO_MODULE.search(section)
        blocked = _HOST_BLOCKED_DLL.search(section)
        if found and not _is_local_module(workspace, found.group(1)):
            missing[wanted[path]] = (
                f"collection failed: No module named '{found.group(1)}', an "
                "optional dependency the run environment does not have"
            )
        elif blocked:
            missing[wanted[path]] = (
                f"collection failed: this host's Application Control blocks the "
                f"compiled module '{blocked.group(1)}'; CI runs it"
            )
    return missing


def verification_deselects(run_directory: Path) -> list[str]:
    """Node IDs the run's recorded verification command deselects.

    `build-prompts` freezes that command before the primary starts, and the
    baseline check runs it on the clean base tree, so a deselect there names a
    test that already failed on this host without the diff (nox#302: three
    tests Application Control kills). It cannot have been chosen to hide what
    the diff broke. See Mailman #161.
    """
    path = run_directory / "prompts.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    argv = payload.get("verification_command") if isinstance(payload, dict) else None
    if not isinstance(argv, list):
        return []
    found: list[str] = []
    for index, argument in enumerate(argv):
        if not isinstance(argument, str):
            continue
        if argument == "--deselect" and index + 1 < len(argv):
            value = argv[index + 1]
        elif argument.startswith("--deselect="):
            value = argument.split("=", 1)[1]
        else:
            continue
        if isinstance(value, str) and value:
            found.append(value)
    return found



def verification_marker(run_directory: Path) -> str | None:
    """The `-m` expression the run's recorded pytest verification passes.

    Same reasoning as `verification_deselects`: the baseline ran that command
    on the clean base tree. nicegui verified with `-m 'not screen'` because
    this host has no Chrome, and touched tests without it hung on
    chromedriver. See Mailman #278.
    """
    try:
        payload = json.loads((run_directory / "prompts.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    argv = payload.get("verification_command") if isinstance(payload, dict) else None
    if not isinstance(argv, list) or "pytest" not in argv:
        return None
    arguments = argv[argv.index("pytest") + 1:]
    for index, argument in enumerate(arguments):
        if argument == "-m" and index + 1 < len(arguments):
            value = arguments[index + 1]
            return value if isinstance(value, str) and value else None
    return None


#: Options that say how pytest is configured, not what it runs. The value
#: options take the next argument unless written `--name=value`.
_SETTINGS_VALUE_OPTIONS = frozenset({
    "--ds", "--dc", "-c", "--config-file", "--rootdir", "-p", "-o",
    "--override-ini", "--import-mode", "--confcutdir",
})
_SETTINGS_FLAGS = frozenset({"--nomigrations", "--no-migrations", "--create-db", "--reuse-db"})


def verification_settings(run_directory: Path) -> list[str]:
    """The configuration options the run's recorded pytest verification passes.

    django-oauth-toolkit sets DJANGO_SETTINGS_MODULE only in tox, so its
    verification passed `--ds=tests.settings`; touched tests ran without it
    and every file failed to import the conftest. The cache plugin option is
    left to the caller, which chooses its own. Mailman #411.
    """
    try:
        payload = json.loads((run_directory / "prompts.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    argv = payload.get("verification_command") if isinstance(payload, dict) else None
    if not isinstance(argv, list) or "pytest" not in argv:
        return []
    arguments = [part for part in argv[argv.index("pytest") + 1:] if isinstance(part, str)]
    kept: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        name = argument.split("=", 1)[0]
        if argument in _SETTINGS_VALUE_OPTIONS and index + 1 < len(arguments):
            pair = [argument, arguments[index + 1]]
            if pair != list(NO_CACHE):
                kept += pair
            index += 2
            continue
        if (name in _SETTINGS_VALUE_OPTIONS and "=" in argument) or argument in _SETTINGS_FLAGS:
            kept.append(argument)
        index += 1
    return kept


def deselects_for(run_directory: Path, paths: list[str]) -> list[str]:
    """The verification deselects whose test file is among `paths`."""
    wanted = {path.replace("\\", "/") for path in paths}
    return [
        node for node in verification_deselects(run_directory)
        if node.split("::", 1)[0].replace("\\", "/") in wanted
    ]

def _write(run_directory: Path, record: dict[str, Any]) -> dict[str, Any]:
    (run_directory / TOUCHED_TESTS_FILENAME).write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return record


def load_touched_tests(run_directory: Path) -> dict[str, Any] | None:
    path = run_directory / TOUCHED_TESTS_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


_FAILING_NODE = re.compile(r"^(?:FAILED|ERROR) (\S+?)(?: - .*)?$", re.MULTILINE)
BASELINE_NODE_LIMIT = 50
TOUCHED_BASELINE_DIRECTORY = "touched-base"
#: Two tokens either way, so the slice after them stays the same length.
NO_CACHE = ("-p", "no:cacheprovider")
_UNKNOWN_CACHE_DIR = "Unknown config option: cache_dir"
_NO_CACHE_ATTRIBUTE = "object has no attribute 'cache'"


def needs_scratch_cache(exit_code: int, output: str) -> bool:
    """Whether a run under `NO_CACHE` failed because the cache plugin was off.

    A project that sets `cache_dir` under --strict-config exits 4 (#297); a
    plugin that reads `config.cache`, pytest-pyvista, fails after every test
    passed (#432).
    """
    if exit_code == 4 and _UNKNOWN_CACHE_DIR in output:
        return True
    return exit_code != 0 and _NO_CACHE_ATTRIBUTE in output


def failing_nodes(output: str) -> list[str]:
    """The node ids pytest's short summary names as failed or errored."""
    return sorted({match.group(1) for match in _FAILING_NODE.finditer(output)})


def _baseline_failures(
    run_directory: Path,
    *,
    workspace: Path,
    base_commit: str,
    python: str,
    nodes: list[str],
    timeout_seconds: float,
    files: list[str] | None = None,
    extra: list[str] | None = None,
    directory: str = ".",
    cache: tuple[str, str] = NO_CACHE,
    settings: list[str] | None = None,
) -> dict[str, Any]:
    """Run the failing nodes on the base commit and say which fail there too.

    Past BASELINE_NODE_LIMIT nodes the same `files` run instead, with the
    candidate run's `extra` arguments, because thousands of node ids overflow
    a Windows command line. scverse/anndata#2348 (#202): 2249 nodes failed
    for want of awkward, at the base commit as well.

    pydata/xarray#10639 (#180): three netCDF datatree tests failed for want of
    a netCDF4 build the host cannot load, at the base commit as well as with
    the patch, and blocked a zarr-only change.

    A node that passes at the base commit is run once more on the candidate;
    one that passes then is recorded as flaky, not new. pyinstaller#9121
    (#183): a onefile build test failed once with an OSError, passed at base
    and passed twice more with the patch, which never reached its code.
    """
    from mailman.target_checks import _Baseline

    record: dict[str, Any] = {
        "base_commit": base_commit,
        "failing": nodes,
        "failing_at_base": [],
        "new": nodes,
        "flaky": [],
        "detail": "",
    }
    baseline = _Baseline(
        run_directory, workspace, base_commit, directory=TOUCHED_BASELINE_DIRECTORY
    )
    try:
        if not baseline.prepare(timeout_seconds):
            record["detail"] = f"the base worktree could not be made: {baseline.detail}"
            return record
        ran = execute(
            _pytest_targets(python, nodes, files, extra, cache, settings),
            working_directory=baseline.path / directory,
            timeout_seconds=timeout_seconds,
        )
        if ran.timed_out:
            record["detail"] = "the base run timed out"
            return record
        at_base = set(failing_nodes(ran.stdout + "\n" + ran.stderr))
        record["failing_at_base"] = [node for node in nodes if node in at_base]
        record["new"] = [node for node in nodes if node not in at_base]
        record["detail"] = f"the base run exited {ran.exit_code}"
    finally:
        baseline.close()
    if record["new"]:
        again = execute(
            _pytest_targets(python, record["new"], files, extra, cache, settings),
            working_directory=workspace / directory,
            timeout_seconds=timeout_seconds,
        )
        if not again.timed_out and again.exit_code in (0, 1):
            still = set(failing_nodes(again.stdout + "\n" + again.stderr))
            record["flaky"] = [node for node in record["new"] if node not in still]
            record["new"] = [node for node in record["new"] if node in still]
            record["detail"] += f"; the candidate rerun exited {again.exit_code}"
    return record


def _scratch_cache(run_directory: Path) -> tuple[str, str]:
    """The cache plugin kept on, writing under the run's scratch directory.

    A project that sets `cache_dir` under --strict-config exits 4 once the
    plugin that registers the option is disabled: beets' setup.cfg. Mailman #297.
    """
    return ("-o", f"cache_dir={run_directory / 'scratch' / 'pytest-cache'}")


def _pytest_targets(
    python: str,
    nodes: list[str],
    files: list[str] | None,
    extra: list[str] | None,
    cache: tuple[str, str] = NO_CACHE,
    settings: list[str] | None = None,
) -> list[str]:
    if len(nodes) <= BASELINE_NODE_LIMIT or not files:
        return [python, "-m", "pytest", *nodes, "-q", *cache, *(settings or []), "-rfE"]
    return [python, "-m", "pytest", *files, "-q", *cache, *(extra or []), "-rfE"]


def _listed(nodes: list[str], limit: int = 10) -> str:
    shown = ", ".join(nodes[:limit])
    return shown + (f" and {len(nodes) - limit} more" if len(nodes) > limit else "")


# --- CI marker lane (#302) --------------------------------------------------
#
# Direct-importer selection misses a test that reaches the changed module
# through the library's public API. edgartools#1386 changed a parser internal;
# 18 tests under `tests/issues/regression/` reached it through `Filing` and
# failed CI's `pytest -n auto -m 'fast'` lane, and none of them was selected.
# When the target's CI runs such a lane, it runs here too, on the candidate
# and, if anything fails, on the base commit; a test that fails only with the
# patch is a regression and blocks like a touched-test failure.

MARKER_LANE_TIMEOUT_SECONDS = 10 * 60
#: Overrides the default cap, in seconds, for each lane run.
MARKER_LANE_CAP_VARIABLE = "MAILMAN_MARKER_LANE_CAP_SECONDS"
MARKER_LANE_BASE_DIRECTORY = "lane-base"
_PYTEST_PROGRAM = re.compile(r"(?:^|[/\\])(?:py\.test|pytest)(?:\.exe)?$")
_SHELL_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})
#: pytest options whose value is the next token, so that token is no path.
_PYTEST_VALUE_OPTIONS = frozenset(
    {
        "-k", "-p", "-c", "-o", "-W", "--deselect", "--ignore", "--ignore-glob",
        "--rootdir", "--basetemp", "--confcutdir", "--junitxml", "--junit-xml",
        "--durations", "--maxfail", "--timeout", "--dist", "--cov", "--cov-report",
        "--cov-config", "--cov-fail-under", "--log-level", "--log-cli-level",
        "--log-file", "--import-mode", "--tb", "--color", "--reruns",
        "--reruns-delay", "--splits", "--group", "--randomly-seed", "-r",
        "--doctest-glob", "--override-ini", "--capture", "--html", "--benchmark-json",
    }
)


def marker_lane_cap_seconds() -> float:
    """The per-run cap: the environment's override, or ten minutes."""
    try:
        value = float(os.environ.get(MARKER_LANE_CAP_VARIABLE, ""))
    except ValueError:
        return MARKER_LANE_TIMEOUT_SECONDS
    return value if value > 0 else MARKER_LANE_TIMEOUT_SECONDS


def _workflow_scripts(text: str) -> list[tuple[int, str]]:
    """`(line number, script)` for each `run:` step of a workflow file."""
    from mailman.target_checks import _YAML_KEY, _yaml_scalar

    lines = text.splitlines()
    anchors: dict[str, str | list[str]] = {}
    scripts: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        found = _YAML_KEY.match(lines[index])
        index += 1
        if not found or found.group(3) != "run":
            continue
        spaces, dash, _, value = found.groups()
        line_number = index
        script, index = _yaml_scalar(
            (value or "").strip(), lines, index, len(spaces) + len(dash or ""), anchors
        )
        if script:
            scripts.append((line_number, script))
    return scripts


def _pytest_lane(command: str, workspace: Path) -> dict[str, Any] | None:
    """The marker, xdist value and paths of a `pytest ... -m EXPR` command."""
    import shlex

    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    start = next(
        (position for position, token in enumerate(tokens) if _PYTEST_PROGRAM.search(token)),
        None,
    )
    if start is None:
        return None
    marker = xdist = None
    paths: list[str] = []
    position = start + 1
    while position < len(tokens):
        token = tokens[position]
        following = tokens[position + 1] if position + 1 < len(tokens) else None
        position += 1
        if token in _SHELL_SEPARATORS:
            break
        if token == "-m" and following is not None:
            marker, position = following, position + 1
        elif token in ("-n", "--numprocesses") and following is not None:
            xdist, position = following, position + 1
        elif token.startswith("--numprocesses="):
            xdist = token.split("=", 1)[1]
        elif token.startswith("-n") and len(token) > 2 and not token.startswith("--"):
            xdist = token[2:]
        elif token in _PYTEST_VALUE_OPTIONS:
            position += 1
        elif not token.startswith("-") and (workspace / token.split("::", 1)[0]).exists():
            paths.append(token.replace("\\", "/"))
    # A marker from a matrix variable is no expression Mailman can run.
    if not marker or "$" in marker:
        return None
    return {"marker": marker, "xdist": xdist if xdist and "$" not in xdist else None,
            "paths": paths}


def marker_lanes(workspace: Path | None) -> list[dict[str, Any]]:
    """Every distinct pytest marker lane the target's GitHub workflows run.

    edgartools' `test-fast` job runs `pytest -n auto -m 'fast'`. A lane is the
    `-m` expression with any test paths after it; the first one found runs.
    """
    if workspace is None:
        return []
    workflows = workspace / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    lanes: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for path in sorted(workflows.iterdir()):
        if path.suffix not in (".yml", ".yaml") or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line_number, script in _workflow_scripts(text):
            for command in re.sub(r"\\\r?\n", " ", script).splitlines():
                if "pytest" not in command and "py.test" not in command:
                    continue
                lane = _pytest_lane(command, workspace)
                if lane is None:
                    continue
                key = (lane["marker"], tuple(lane["paths"]))
                if key in seen:
                    continue
                seen.add(key)
                lanes.append(
                    {
                        "workflow": f".github/workflows/{path.name}",
                        "line": line_number,
                        "command": command.strip(),
                        **lane,
                    }
                )
    return lanes


def _import_path_environment(tree: Path, workspace: Path) -> dict[str, str]:
    """`PYTHONPATH` naming `tree`'s own source first.

    An editable install points at the candidate workspace; without this a src
    layout's base run would import the patched code and every regression
    would read as failing at base too.
    """
    entries = [str(tree)]
    for root in ("src", *pytest_import_roots(workspace)):
        if (tree / root).is_dir():
            entries.append(str(tree / root))
    inherited = os.environ.get("PYTHONPATH")
    if inherited:
        entries.append(inherited)
    return {"PYTHONPATH": os.pathsep.join(dict.fromkeys(entries))}


_FIND_ORIGIN = (
    "import importlib.util, sys\n"
    "spec = importlib.util.find_spec(sys.argv[1])\n"
    "locations = list(spec.submodule_search_locations or []) if spec else []\n"
    "print(spec.origin if spec and spec.origin else (locations[0] if locations else ''))\n"
)


def _top_package(modules: dict[str, list[str]]) -> str | None:
    for names in modules.values():
        if names:
            return names[0].split(".", 1)[0]
    return None


def _inside(path: str, directory: Path) -> bool:
    try:
        Path(path).resolve().relative_to(directory.resolve())
    except (OSError, ValueError):
        return False
    return True


def run_marker_lane(
    run_directory: Path,
    *,
    workspace: Path,
    python: str | None,
    base_commit: str | None,
    modules: dict[str, list[str]],
    cap_seconds: float | None = None,
) -> dict[str, Any]:
    """Run the target's CI marker lane on the candidate and, on failure, at base.

    The result is `status` `ran`, or `fallback` with a `reason`: direct-importer
    selection is then the only evidence, and the record says why.
    """
    cap = cap_seconds if cap_seconds and cap_seconds > 0 else marker_lane_cap_seconds()
    lanes = marker_lanes(workspace)
    record: dict[str, Any] = {
        "status": "fallback",
        "reason": None,
        "detail": "",
        "lanes": lanes,
        "lane": None,
        "cap_seconds": cap,
        "marker_filter": None,
        "deselected": [],
        "command": None,
        "exit_code": None,
        "timed_out": False,
        "duration_seconds": 0.0,
        "passed": None,
        "failed": None,
        "errors": None,
        "skipped": None,
        "failing": [],
        "base_commit": base_commit,
        "base_command": None,
        "base_exit_code": None,
        "base_imports": None,
        "failing_at_base": [],
        "regressions": [],
        "flaky": [],
        "output_tail": "",
    }

    def fallback(reason: str, detail: str) -> dict[str, Any]:
        record["status"], record["reason"], record["detail"] = "fallback", reason, detail
        return record

    if not lanes:
        return fallback("no-lane", "no workflow runs pytest with a `-m` marker expression")
    lane = record["lane"] = lanes[0]
    if python is None:
        return fallback("no-environment-python", "the run environment has no interpreter")
    if not base_commit:
        return fallback("no-base-commit", "failures cannot be compared without a base commit")
    expressions = [lane["marker"]]
    if network_marker_registered(workspace):
        expressions.append("not network")
    frozen = verification_marker(run_directory)
    if frozen:
        expressions.append(frozen)
    marker_filter = (
        " and ".join(f"({expression})" for expression in expressions)
        if len(expressions) > 1 else expressions[0]
    )
    record["marker_filter"] = marker_filter
    deselected = verification_deselects(run_directory)
    record["deselected"] = deselected
    xdist: list[str] = []
    if lane.get("xdist"):
        probe = execute(
            [python, "-c", "import xdist"],
            working_directory=workspace,
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
        )
        # Left out when the environment lacks pytest-xdist: slower, not wrong.
        if probe.exit_code == 0 and not probe.timed_out:
            xdist = ["-n", lane["xdist"]]
    record["xdist"] = xdist[1] if xdist else None
    cache = NO_CACHE

    def lane_command() -> list[str]:
        return [
            python, "-m", "pytest", *lane["paths"], "-q", *cache, "-rfE",
            "--continue-on-collection-errors", "-m", marker_filter, *xdist,
            *(argument for node in deselected for argument in ("--deselect", node)),
        ]

    while True:
        command = lane_command()
        result = execute(
            command,
            working_directory=workspace,
            timeout_seconds=cap,
            environment=_import_path_environment(workspace, workspace),
        )
        output = result.stdout + "\n" + result.stderr
        if (
            cache == NO_CACHE and not result.timed_out
            and needs_scratch_cache(result.exit_code, output)
        ):
            cache = _scratch_cache(run_directory)
            continue
        break
    record["command"] = command
    record["exit_code"] = result.exit_code
    record["timed_out"] = result.timed_out
    record["duration_seconds"] = result.duration_seconds
    record.update(parse_counts("pytest", result.stdout, result.stderr))
    record["output_tail"] = _tail(output)
    if result.timed_out:
        return fallback("exceeded-cap", f"the lane ran past the {cap:.0f} s cap")
    if result.exit_code == 5:
        return fallback("collected-nothing", "the marker expression selected no test")
    if result.exit_code not in (0, 1):
        return fallback(
            "not-collected", f"pytest exited {result.exit_code} before running the lane"
        )
    record["failing"] = failing_nodes(output)
    if result.exit_code == 0 or not record["failing"]:
        record["status"] = "ran"
        return record

    from mailman.target_checks import _Baseline

    baseline = _Baseline(
        run_directory, workspace, base_commit, directory=MARKER_LANE_BASE_DIRECTORY
    )
    try:
        if not baseline.prepare(cap):
            return fallback(
                "base-worktree-failed", f"the base worktree could not be made: {baseline.detail}"
            )
        environment = _import_path_environment(baseline.path, workspace)
        package = _top_package(modules)
        if package:
            found = execute(
                [python, "-c", _FIND_ORIGIN, package],
                working_directory=baseline.path,
                timeout_seconds=PROBE_TIMEOUT_SECONDS,
                environment=environment,
            )
            origin = found.stdout.strip().splitlines()[-1] if found.stdout.strip() else ""
            record["base_imports"] = origin
            if not origin or not _inside(origin, baseline.path):
                return fallback(
                    "base-imports-elsewhere",
                    f"at the base commit `{package}` imports from {origin or 'nowhere'}, "
                    "not the base tree, so the comparison would prove nothing",
                )
        base_command = list(command)
        record["base_command"] = base_command
        ran = execute(
            base_command,
            working_directory=baseline.path,
            timeout_seconds=cap,
            environment=environment,
        )
        record["base_exit_code"] = ran.exit_code
        if ran.timed_out:
            return fallback("base-exceeded-cap", f"the base run ran past the {cap:.0f} s cap")
        if ran.exit_code not in (0, 1):
            return fallback(
                "base-not-collected", f"pytest exited {ran.exit_code} on the base tree"
            )
    finally:
        baseline.close()
    at_base = set(failing_nodes(ran.stdout + "\n" + ran.stderr))
    record["failing_at_base"] = [node for node in record["failing"] if node in at_base]
    # A test that does not exist at base and fails with the patch counts too.
    new = [node for node in record["failing"] if node not in at_base]
    if new and len(new) <= BASELINE_NODE_LIMIT:
        again = execute(
            _pytest_targets(python, new, None, None, cache),
            working_directory=workspace,
            timeout_seconds=cap,
            environment=_import_path_environment(workspace, workspace),
        )
        if not again.timed_out and again.exit_code in (0, 1):
            still = set(failing_nodes(again.stdout + "\n" + again.stderr))
            record["flaky"] = [node for node in new if node not in still]
            new = [node for node in new if node in still]
    record["regressions"] = new
    record["status"] = "ran"
    return record


def _lane_summary(lane: dict[str, Any] | None) -> str:
    if not lane:
        return ""
    if lane.get("status") == "ran":
        chosen = lane.get("lane") or {}
        return (
            f"; CI marker lane `-m {chosen.get('marker')}` passed {lane.get('passed')}"
            + (
                f", {len(lane['failing_at_base'])} failing at base too"
                if lane.get("failing_at_base") else ""
            )
            + (f", flaky: {_listed(lane['flaky'])}" if lane.get("flaky") else "")
        )
    return f"; CI marker lane not used ({lane.get('reason')}): {lane.get('detail')}"


def run_touched_tests(
    run_directory: Path,
    *,
    diff: str,
    changed_paths: list[str],
    workspace: Path | None,
    timeout_seconds: float = TOUCHED_TESTS_TIMEOUT_SECONDS,
    cap: int = TOUCHED_TESTS_CAP,
    base_commit: str | None = None,
    lane_cap_seconds: float | None = None,
) -> dict[str, Any]:
    """Select, run and record. Every outcome is written, including not running.

    Once the diff changes source, the target's CI marker lane runs too, under
    `marker_lane` (#302).
    """
    started = datetime.now(UTC)
    record: dict[str, Any] = {
        "schema_version": TOUCHED_TESTS_SCHEMA_VERSION,
        "code_version": TOUCHED_TESTS_CODE_VERSION,
        "diff_sha256": diff_sha256(diff),
        "started_at": started.isoformat(),
        "ran": False,
        "reason": None,
        "workspace": str(workspace) if workspace else None,
        "python": None,
        "runner": None,
        "source_files": [],
        "modules": {},
        "candidates": 0,
        "selected": [],
        "capped": False,
        "cap": cap,
        "omitted": [],
        "omitted_reasons": {},
        "collection_retries": [],
        "command": None,
        "marker_filter": None,
        "deselected": [],
        "exit_code": None,
        "timed_out": False,
        "duration_seconds": 0.0,
        "passed": None,
        "failed": None,
        "errors": None,
        "skipped": None,
        "output_tail": "",
        # Failures compared with the base commit; None when not compared.
        "baseline": None,
        # The CI marker lane; None when the diff changes no source.
        "marker_lane": None,
    }
    if workspace is None or not workspace.is_dir():
        record["reason"] = "no-workspace"
        return _write(run_directory, record)
    selection = select_test_files(workspace, changed_paths, cap=cap)
    record.update(selection)
    if not selection["source_files"]:
        record["ran"] = True
        record["reason"] = "no-source-change"
        return _write(run_directory, record)

    def with_lane(python: str | None) -> dict[str, Any]:
        record["marker_lane"] = run_marker_lane(
            run_directory,
            workspace=workspace,
            python=python,
            base_commit=base_commit,
            modules=selection["modules"],
            cap_seconds=lane_cap_seconds,
        )
        return _write(run_directory, record)

    if not selection["selected"]:
        record["ran"] = True
        record["reason"] = "no-matching-tests"
        # The tests that reach the module only through the public API are the
        # ones this selection cannot see; the lane is all the evidence there is.
        return with_lane(environment_python(run_directory))
    python = environment_python(run_directory)
    if python is None:
        record["reason"] = "no-environment-python"
        return with_lane(None)
    record["python"] = python
    probe: CommandResult = execute(
        [python, "-c", "import pytest"],
        working_directory=workspace,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    runner = "pytest" if probe.exit_code == 0 and not probe.timed_out else "unittest"
    files = [entry["path"] for entry in selection["selected"]]
    expressions = []
    if runner == "pytest" and network_marker_registered(workspace):
        expressions.append("not network")
    frozen = verification_marker(run_directory) if runner == "pytest" else None
    if frozen:
        expressions.append(frozen)
    marker_filter = (
        " and ".join(f"({expression})" for expression in expressions)
        if len(expressions) > 1 else (expressions[0] if expressions else None)
    )
    marker = ["-m", marker_filter] if marker_filter else []
    if marker_filter:
        record["marker_filter"] = marker_filter

    def deselect_for(paths: list[str]) -> list[str]:
        kept = deselects_for(run_directory, paths)
        record["deselected"] = kept
        return [argument for node in kept for argument in ("--deselect", node)]

    cache = NO_CACHE
    settings = verification_settings(run_directory) if runner == "pytest" else []

    def command_for(paths: list[str]) -> list[str]:
        if runner == "pytest":
            return [
                python, "-m", "pytest", *paths, "-q", *cache,
                *settings, *marker, *deselect_for(paths),
            ]
        return [python, "-m", "unittest", *paths]

    record["runner"] = runner
    record["omitted_reasons"] = {}
    record["collection_retries"] = []
    while True:
        command = command_for(files)
        record["command"] = command
        result: CommandResult = execute(
            command, working_directory=workspace, timeout_seconds=timeout_seconds
        )
        if runner != "pytest" or result.timed_out:
            break
        output = result.stdout + "\n" + result.stderr
        if cache == NO_CACHE and needs_scratch_cache(result.exit_code, output):
            cache = _scratch_cache(run_directory)
            record["cache_option"] = list(cache)
            continue
        if result.exit_code not in (2, 4):
            break
        # A test file whose import needs an optional extra the environment
        # lacks, or a DLL this host blocks, is left out with its reason, and
        # the rest run again (#127, #214).
        missing = missing_extra_collection_errors(
            result.stdout + "\n" + result.stderr, workspace, files
        )
        if not missing:
            break
        record["collection_retries"].append(
            {"command": command, "exit_code": result.exit_code, "omitted": sorted(missing)}
        )
        for path, reason in missing.items():
            record["omitted"].append(path)
            record["omitted_reasons"][path] = reason
        record["selected"] = [
            entry for entry in record["selected"] if entry["path"] not in missing
        ]
        files = [path for path in files if path not in missing]
        if not files:
            record["exit_code"] = result.exit_code
            record["output_tail"] = _tail(result.stdout + "\n" + result.stderr)
            record["reason"] = "all-selected-omitted"
            return with_lane(python)
    # Suites that open data relative to their own directory fail wholesale from
    # the root: biopython's CI runs `cd Tests`. Rerun from the one directory
    # the files share and keep that run only if it fails strictly fewer (#215).
    directory = _shared_directory(files)
    record["working_directory"] = "."
    if runner == "pytest" and directory and result.exit_code == 1 and not result.timed_out:
        local = [PurePosixPath(path).relative_to(directory).as_posix() for path in files]
        local_command = command_for(local)
        moved = execute(
            local_command, working_directory=workspace / directory,
            timeout_seconds=timeout_seconds,
        )
        root_counts = parse_counts(runner, result.stdout, result.stderr)
        moved_counts = parse_counts(runner, moved.stdout, moved.stderr)
        if (
            not moved.timed_out
            and moved.exit_code in (0, 1)
            and _unsuccessful(moved_counts) < _unsuccessful(root_counts)
        ):
            record["directory_retry"] = {
                "directory": directory, "root": root_counts, "moved": moved_counts,
            }
            record["working_directory"] = directory
            result, command, files = moved, local_command, local
            record["command"] = command
        else:
            command = command_for(files)
    record["ran"] = True
    record["exit_code"] = result.exit_code
    record["timed_out"] = result.timed_out
    record["duration_seconds"] = result.duration_seconds
    record.update(parse_counts(runner, result.stdout, result.stderr))
    record["output_tail"] = _tail(result.stdout + "\n" + result.stderr)
    nodes = failing_nodes(result.stdout)
    if (
        runner == "pytest"
        and base_commit
        and result.exit_code == 1
        and not result.timed_out
        and nodes
    ):
        record["baseline"] = _baseline_failures(
            run_directory,
            workspace=workspace,
            base_commit=base_commit,
            python=python,
            nodes=nodes,
            timeout_seconds=timeout_seconds,
            files=files,
            # The settings, marker filter and deselects: what follows `-q`
            # and the two cache tokens.
            extra=command[3 + len(files) + 3:],
            directory=record["working_directory"],
            cache=cache,
            settings=settings,
        )
    return with_lane(python)


def touched_tests_verdict(
    record: dict[str, Any] | None, *, expected_diff_sha256: str | None = None
) -> tuple[str | None, str]:
    """`(code, detail)` for a record; the code is None when the stage passed.

    `touched-tests-not-run` means nobody has the evidence yet; a record for
    another diff is as good as none. `touched-tests-failed` means the evidence
    exists and says no.
    """
    if record is None:
        return (
            "touched-tests-not-run",
            "the tests that exercise the changed modules have not been run for "
            "this export. Run `mailman prepare-submission` with the run's "
            "workspace and environment in place.",
        )
    if expected_diff_sha256 and record.get("diff_sha256") != expected_diff_sha256:
        return (
            "touched-tests-not-run",
            "the touched-tests record belongs to an earlier export; run "
            "`mailman prepare-submission` again for the current diff.",
        )
    if record.get("reason") == "all-selected-omitted":
        return (
            "touched-tests-not-run",
            "every selected test file failed to collect on a missing optional "
            "dependency, so nothing ran: "
            + "; ".join(
                f"{path}: {why}"
                for path, why in (record.get("omitted_reasons") or {}).items()
            )
            + ". Install the extra with `mailman prepare-environment` and rerun.",
        )
    if not record.get("ran"):
        reason = record.get("reason") or "unknown"
        return (
            "touched-tests-not-run",
            f"the touched-tests stage did not run ({reason}). It needs the run's "
            "workspace and the interpreter `prepare-environment` registered.",
        )
    lane = record.get("marker_lane") if isinstance(record.get("marker_lane"), dict) else None
    if lane and lane.get("regressions"):
        chosen = lane.get("lane") or {}
        return (
            "touched-tests-failed",
            f"the target's CI marker lane `-m {chosen.get('marker')}` "
            f"({chosen.get('workflow')}) fails tests that pass at the base commit: "
            f"{_listed(lane['regressions'])}. Command: {' '.join(lane.get('command') or [])}",
        )
    if record.get("reason") in _NOTHING_TO_RUN:
        return None, f"nothing to run: {record['reason']}" + _lane_summary(lane)
    if record.get("timed_out"):
        return (
            "touched-tests-failed",
            f"the touched tests timed out after {record.get('duration_seconds')} s: "
            f"{' '.join(record.get('command') or [])}",
        )
    baseline = record.get("baseline") or {}
    if (
        record.get("exit_code") == 1
        and baseline.get("failing")
        and not baseline.get("new")
        and "new" in baseline
    ):
        parts = []
        if baseline.get("failing_at_base"):
            parts.append(
                "these also fail at the base commit: "
                + _listed(baseline["failing_at_base"])
            )
        if baseline.get("flaky"):
            parts.append(
                "these pass at the base commit and on a second run of the "
                "candidate, so they are flaky: " + _listed(baseline["flaky"])
            )
        return (
            None,
            f"passed {record.get('passed')}; the patch adds no failure. "
            + "; ".join(parts)
            + _lane_summary(lane),
        )
    if record.get("exit_code") != 0:
        new = (
            baseline.get("new")
            if baseline.get("failing_at_base") or baseline.get("flaky")
            else None
        )
        return (
            "touched-tests-failed",
            "the tests that exercise the changed modules failed "
            f"(exit {record.get('exit_code')}, passed {record.get('passed')}, "
            f"failed {record.get('failed')}, errors {record.get('errors')}): "
            f"{' '.join(record.get('command') or [])}"
            + (f". New since the base commit: {_listed(new)}" if new else ""),
        )
    indirect = record.get("indirect") or []
    return (
        None,
        f"{len(record.get('selected') or [])} test file(s) ran, "
        f"passed {record.get('passed')}, failed {record.get('failed')}"
        + (
            f"; left out {len(indirect)} file(s) that name only the package from "
            f"outside it: {', '.join(indirect)}"
            if indirect
            else ""
        )
        + _lane_summary(lane),
    )


def resolve_workspace(run_directory: Path, given: Path | None) -> Path | None:
    """The workspace the export came from, or the one `prepare-workspace` made."""
    if given is not None:
        return given
    export = run_directory / "export" / "export.json"
    if export.is_file():
        try:
            payload = json.loads(export.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        recorded = payload.get("workspace") if isinstance(payload, dict) else None
        if isinstance(recorded, str) and Path(recorded).is_dir():
            return Path(recorded)
    prepared = run_directory / "workspace"
    return prepared if prepared.is_dir() else None


__all__ = [
    "MARKER_LANE_TIMEOUT_SECONDS",
    "TOUCHED_TESTS_CAP",
    "TOUCHED_TESTS_FILENAME",
    "TOUCHED_TESTS_TIMEOUT_SECONDS",
    "diff_sha256",
    "environment_python",
    "load_touched_tests",
    "marker_lanes",
    "module_names",
    "parse_counts",
    "resolve_workspace",
    "run_marker_lane",
    "run_touched_tests",
    "select_test_files",
    "touched_tests_verdict",
]
