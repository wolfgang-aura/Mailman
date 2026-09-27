from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path


def packaged_npm_roots(local_app_data: str | None = None) -> list[Path]:
    """Return npm global folders that only exist inside an MSIX package.

    A CLI installed with `npm install -g` from a packaged app, such as the
    Claude desktop app's terminal, lands in the package's virtualized
    ``%APPDATA%\\npm``. Processes inside the package see it at the usual path;
    Mailman, started as an ordinary process, does not, so `shutil.which` found
    no claude on a host where the operator uses it every day. The real files
    sit under ``%LOCALAPPDATA%\\Packages\\<package>\\LocalCache\\Roaming\\npm``.
    """
    base = local_app_data if local_app_data is not None else os.environ.get("LOCALAPPDATA")
    if not base:
        return []
    packages = Path(base) / "Packages"
    if not packages.is_dir():
        return []
    return sorted(
        root
        for root in packages.glob("*/LocalCache/Roaming/npm")
        if root.is_dir()
    )


def find_executable(
    name: str,
    *,
    windows: bool | None = None,
    local_app_data: str | None = None,
) -> str | None:
    """Find a bare command on PATH, then in packaged npm folders on Windows."""
    found = shutil.which(name)
    if found is not None:
        return found
    if windows is None:
        windows = platform.system() == "Windows"
    if not windows or Path(name).name != name:
        return None
    extensions = [""] if Path(name).suffix else [".exe", ".cmd", ".bat"]
    for root in packaged_npm_roots(local_app_data):
        for extension in extensions:
            candidate = root / f"{name}{extension}"
            if candidate.is_file():
                return str(candidate)
    return None
