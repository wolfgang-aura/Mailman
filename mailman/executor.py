from __future__ import annotations

import os
import platform
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from mailman.redaction import redact


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    working_directory: str
    started_at: str
    duration_seconds: float
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    timeout_seconds: float
    environment: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _environment_metadata() -> dict[str, str]:
    return {
        "operating_system": platform.system(),
        "operating_system_release": platform.release(),
        "machine": platform.machine(),
        "python_version": platform.python_version(),
    }


def execute(
    command: Sequence[str],
    *,
    working_directory: Path,
    timeout_seconds: float = 900,
    environment: Mapping[str, str] | None = None,
    stdin_text: str | None = None,
) -> CommandResult:
    """Run a command without a shell and return a redacted evidence record."""
    if not command:
        raise ValueError("command cannot be empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    cwd = working_directory.resolve(strict=True)
    started = datetime.now(UTC).isoformat()
    start_clock = time.monotonic()
    process_environment = os.environ.copy()
    if environment:
        process_environment.update(environment)

    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=process_environment,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        timed_out = False
    except subprocess.TimeoutExpired as error:
        exit_code = None
        stdout = error.stdout or ""
        stderr = error.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        timed_out = True

    return CommandResult(
        command=[redact(part) for part in command],
        working_directory=str(cwd),
        started_at=started,
        duration_seconds=round(time.monotonic() - start_clock, 6),
        exit_code=exit_code,
        stdout=redact(stdout),
        stderr=redact(stderr),
        timed_out=timed_out,
        timeout_seconds=timeout_seconds,
        environment=_environment_metadata(),
    )
