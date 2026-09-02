"""What an agent CLI reads besides the prompt Mailman wrote.

Both CLIs load instruction files from the operator's home directory. Those files
are machine-local, so two hosts replaying the same run ID can get different
behavior and nothing in the run record says why. Run
`20260902T144544Z-5dbf69` spent the reviewer's first action reading a personal
writing-style skill.

Mailman cannot stop a vendor CLI from reading them, so it records what was in
effect instead. A silent input is the thing this project exists to eliminate.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


# Instruction files each CLI loads on its own, relative to the operator's home.
INSTRUCTION_FILES: dict[str, tuple[str, ...]] = {
    "claude": (".claude/CLAUDE.md", ".claude/settings.json"),
    "codex": (".codex/AGENTS.md", ".codex/config.toml"),
}

# Skill directories, whose `SKILL.md` files are instructions by another name.
SKILL_DIRECTORIES: dict[str, str] = {
    "claude": ".claude/skills",
    "codex": ".codex/skills",
}

# `--ignore-user-config` covers `config.toml` and nothing else, which is why the
# reviewer still read a personal skill.
SUPPRESSED: dict[str, frozenset[str]] = {
    "codex": frozenset({".codex/config.toml"}),
    "claude": frozenset(),
}


def _digest(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "present": True,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def describe_instruction_sources(
    agent: str, *, home: Path | None = None
) -> list[dict[str, Any]]:
    """Name every instruction source in effect for one agent, with a digest.

    A source Mailman already suppresses is listed too, marked as suppressed, so
    the record says what was ruled out as well as what was read.
    """
    normalized = agent.strip().lower()
    root = home or Path.home()
    suppressed = SUPPRESSED.get(normalized, frozenset())
    sources: list[dict[str, Any]] = []

    for relative in INSTRUCTION_FILES.get(normalized, ()):
        path = root / relative
        entry: dict[str, Any] = {
            "kind": "file",
            "path": str(path),
            "suppressed": relative in suppressed,
            "present": False,
        }
        if path.is_file():
            entry.update(_digest(path))
        sources.append(entry)

    skills_relative = SKILL_DIRECTORIES.get(normalized)
    if skills_relative:
        skills = root / skills_relative
        for skill_file in sorted(skills.glob("*/SKILL.md")) if skills.is_dir() else []:
            entry = {
                "kind": "skill",
                "name": skill_file.parent.name,
                "path": str(skill_file),
                "suppressed": False,
            }
            entry.update(_digest(skill_file))
            sources.append(entry)

    return sources
