from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path

from mailman.executor import CommandResult, execute


_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def _manifest_path(run_directory: Path) -> Path:
    return run_directory / "toolchain.json"


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_toolchain(run_directory: Path) -> dict[str, object]:
    path = _manifest_path(run_directory)
    if not path.is_file():
        return {"schema_version": 1, "tools": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("tools"), dict):
        raise ValueError("unsupported toolchain manifest")
    return data


def probe_tool(
    run_directory: Path,
    *,
    name: str,
    executable: Path,
    probe_arguments: list[str],
    timeout_seconds: float,
) -> CommandResult:
    if not _TOOL_NAME_PATTERN.fullmatch(name):
        raise ValueError("tool name must use lowercase letters, digits, underscores, or hyphens")
    executable_path = executable.resolve(strict=True)
    if not executable_path.is_file():
        raise ValueError("tool executable must be a file")
    result = execute(
        [str(executable_path), *probe_arguments],
        working_directory=run_directory,
        timeout_seconds=timeout_seconds,
    )
    if result.timed_out or result.exit_code != 0:
        return result

    manifest = load_toolchain(run_directory)
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    tools[name] = {
        "executable": str(executable_path),
        "sha256": _file_sha256(executable_path),
        "probe": result.to_dict(),
    }
    destination = _manifest_path(run_directory)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return result


def _verified_executable(name: str, raw_record: object) -> str:
    if not isinstance(raw_record, dict):
        raise ValueError(f"invalid toolchain record: {name}")
    executable = raw_record.get("executable")
    expected_sha256 = raw_record.get("sha256")
    if not isinstance(executable, str) or not isinstance(expected_sha256, str):
        raise ValueError(f"invalid toolchain executable: {name}")
    executable_path = Path(executable).resolve(strict=True)
    if _file_sha256(executable_path) != expected_sha256:
        raise ValueError(f"toolchain executable changed after probe: {name}")
    return executable


def toolchain_executable(run_directory: Path, name: str) -> str | None:
    """Return a probed executable path for one tool once its digest still matches."""
    manifest = load_toolchain(run_directory)
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    if name not in tools:
        return None
    return _verified_executable(name, tools[name])


def prepare_agent_prompt(
    run_directory: Path, *, role: str, source_prompt: Path
) -> Path:
    prompt = source_prompt.read_text(encoding="utf-8")
    manifest = load_toolchain(run_directory)
    tools = manifest["tools"]
    assert isinstance(tools, dict)
    if tools:
        lines = [
            "",
            "## Mailman verified toolchain",
            "",
            "Use these exact executable paths when running repository commands:",
            "",
        ]
        for name, raw_record in sorted(tools.items()):
            executable = _verified_executable(name, raw_record)
            expected_sha256 = raw_record["sha256"]
            lines.append(f"- {name}: `{executable}` (sha256: `{expected_sha256}`)")
        prompt = prompt.rstrip() + "\n" + "\n".join(lines) + "\n"

    prompt_directory = run_directory / "prompts"
    prompt_directory.mkdir(exist_ok=True)
    sequence = len(list(prompt_directory.glob("*.md"))) + 1
    destination = prompt_directory / f"{sequence:04d}-{role}.md"
    destination.write_text(prompt, encoding="utf-8")
    return destination
