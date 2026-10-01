"""Run health states a coordinator must not mistake for a candidate failure.

Both agent tasks in hunt `20260907T164341Z-1ca91a` stopped mid-stage when the
shared model allowance ran out. Neither left a machine-readable resume point,
so the next session reconstructed stage state from run directories by hand.

See https://github.com/wolfgang-aura/Mailman/issues/67.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from mailman.models import utc_now

HEALTH_FILENAME = "health.json"

USAGE_LIMIT = "USAGE_LIMIT"
INFRASTRUCTURE = "INFRASTRUCTURE"

#: Phrases a provider CLI prints when the account, not the candidate, is what
#: stopped the stage. Matched case-insensitively against the agent's stop
#: reason and captured streams.
_USAGE_PATTERNS = (
    r"usage limit",
    r"rate limit",
    r"quota (?:exceeded|exhausted)",
    r"insufficient (?:credit|quota)",
    r"upgrade your plan",
    r"try again at\b",
    r"\b429\b",
)

#: The host, not the code under test, failed. A temporary directory that
#: cannot be created is not a defect in the candidate.
_INFRASTRUCTURE_PATTERNS = (
    r"permission denied: .*(?:temp|tmp)",
    r"\[Errno 13\]",
    r"WinError 5\b",
    r"no space left on device",
    r"could not create cache",
)

_USAGE = re.compile("|".join(_USAGE_PATTERNS), re.IGNORECASE)
_INFRASTRUCTURE = re.compile("|".join(_INFRASTRUCTURE_PATTERNS), re.IGNORECASE)


def agent_errors(stdout: str | None) -> str:
    """The agent CLI's own error lines from its JSON stream.

    The stream also carries every command the agent ran and its output, so a
    `test_x.py:1429` or a target test's `[Errno 13]` anywhere in it read as a
    usage limit or a host failure. Only error events and the lines the CLI
    printed outside the stream are kept. Mailman #353.
    """
    kept: list[str] = []
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            kept.append(stripped)
            continue
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if (kind in {"error", "turn.failed"} or payload.get("error")
                or (kind == "result" and payload.get("is_error"))):
            kept.append(stripped)
    return "\n".join(kept)


def classify(*texts: str | None) -> str | None:
    """Name the non-candidate cause in these texts, if there is one."""
    joined = "\n".join(text for text in texts if text)
    if not joined:
        return None
    if _USAGE.search(joined):
        return USAGE_LIMIT
    if _INFRASTRUCTURE.search(joined):
        return INFRASTRUCTURE
    return None


def record(run_directory: Path, *, state: str, stage: str, resume_command: str,
           detail: str) -> Path:
    destination = run_directory / HEALTH_FILENAME
    destination.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": state,
                "stage": stage,
                "resume_command": resume_command,
                "detail": detail,
                "at": utc_now(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def load(run_directory: Path) -> dict | None:
    path = run_directory / HEALTH_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear(run_directory: Path) -> None:
    (run_directory / HEALTH_FILENAME).unlink(missing_ok=True)
