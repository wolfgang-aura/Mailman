"""Processes still running from a run's environment after the run.

The prefect run 20260930T032102Z-78a763 left a server, a worker and an API
running from its environment for ten hours, and nothing said so. The job
object in the executor stops that; this is the check that shows whether it
held. Mailman #277.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

_QUERY = (
    "Get-CimInstance Win32_Process -Property ProcessId,ExecutablePath,CommandLine"
    " | Where-Object { $_.ExecutablePath -like '*\\environment\\*' }"
    " | Select-Object ProcessId,ExecutablePath,CommandLine"
    " | ConvertTo-Json -Compress"
)


def match_run_environments(processes: list[dict], runs_root: Path) -> list[dict]:
    """The processes whose executable sits under `<runs_root>/<run id>/environment/`."""
    prefix = os.path.normcase(os.path.normpath(str(runs_root.resolve()))) + os.sep
    found = []
    for process in processes:
        executable = os.path.normpath(str(process.get("ExecutablePath") or ""))
        if not os.path.normcase(executable).startswith(prefix):
            continue
        parts = executable[len(prefix):].split(os.sep)
        if len(parts) < 3 or os.path.normcase(parts[1]) != "environment":
            continue
        found.append({
            "run_id": parts[0],
            "pid": process.get("ProcessId"),
            "executable": executable,
            "command_line": (process.get("CommandLine") or "")[:200],
        })
    return found


def leftover_processes(
    runs_root: Path, *, timeout_seconds: float = 20
) -> tuple[list[dict], str | None]:
    """Processes running from any run's environment, and why the check failed if it did."""
    if os.name != "nt":
        return [], None
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _QUERY],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout_seconds, check=False, shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return [], f"process query failed: {error}"
    if completed.returncode:
        return [], f"process query exited {completed.returncode}: {completed.stderr.strip()[:200]}"
    text = completed.stdout.strip()
    if not text:
        return [], None
    try:
        parsed = json.loads(text)
    except ValueError as error:
        return [], f"process query returned unreadable output: {error}"
    rows = parsed if isinstance(parsed, list) else [parsed]
    return match_run_environments([row for row in rows if isinstance(row, dict)], runs_root), None
