from __future__ import annotations

import os
import platform
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Mapping, Sequence

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


def _stream(
    command: Sequence[str],
    *,
    cwd: Path,
    process_environment: dict[str, str],
    stdin_text: str | None,
    timeout_seconds: float,
    on_stdout_line: Callable[[str], None],
) -> tuple[int | None, str, str, bool]:
    """Run a command, handing every stdout line over as it arrives.

    An agent can work for an hour. Buffering its output until it exits means
    nobody can see what it is doing, or tell a slow run from a stuck one.
    """
    process = subprocess.Popen(  # noqa: S603 - the command is a list, no shell
        list(command),
        cwd=cwd,
        env=process_environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        shell=False,
    )
    timed_out = threading.Event()

    def give_up() -> None:
        timed_out.set()
        process.kill()

    watchdog = threading.Timer(timeout_seconds, give_up)
    watchdog.daemon = True
    watchdog.start()

    stderr_lines: list[str] = []

    def drain_stderr() -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            stderr_lines.append(line)

    def write_stdin() -> None:
        # Written from a thread so a large prompt cannot deadlock against a
        # full stdout pipe.
        if process.stdin is None:
            return
        try:
            if stdin_text is not None:
                process.stdin.write(stdin_text)
            process.stdin.close()
        except OSError:
            pass

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stdin_thread = threading.Thread(target=write_stdin, daemon=True)
    stderr_thread.start()
    stdin_thread.start()

    stdout_lines: list[str] = []
    try:
        if process.stdout is not None:
            for line in process.stdout:
                stdout_lines.append(line)
                try:
                    on_stdout_line(line.rstrip("\r\n"))
                except Exception:  # noqa: BLE001 - a broken console must not
                    pass          # cost the run its evidence
        exit_code = process.wait()
    finally:
        watchdog.cancel()
        stdin_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

    if timed_out.is_set():
        exit_code = None
    return exit_code, "".join(stdout_lines), "".join(stderr_lines), timed_out.is_set()


def execute(
    command: Sequence[str],
    *,
    working_directory: Path,
    timeout_seconds: float = 900,
    environment: Mapping[str, str] | None = None,
    stdin_text: str | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
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

    if on_stdout_line is not None:
        exit_code, stdout, stderr, timed_out = _stream(
            command,
            cwd=cwd,
            process_environment=process_environment,
            stdin_text=stdin_text,
            timeout_seconds=timeout_seconds,
            on_stdout_line=on_stdout_line,
        )
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
