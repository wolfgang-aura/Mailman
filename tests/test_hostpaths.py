from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mailman import hostpaths


class FindExecutableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.local = Path(self._tmp.name)
        self.npm = self.local / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "npm"
        self.npm.mkdir(parents=True)
        (self.npm / "claude.cmd").write_text("@echo off\n", encoding="utf-8")
        no_path = mock.patch.object(hostpaths.shutil, "which", return_value=None)
        no_path.start()
        self.addCleanup(no_path.stop)

    def test_packaged_npm_folder_is_searched_on_windows(self) -> None:
        # The Claude desktop app installs npm globals into its MSIX package,
        # which an ordinary process cannot see at %APPDATA%\npm.
        found = hostpaths.find_executable(
            "claude", windows=True, local_app_data=str(self.local)
        )
        self.assertEqual(found, str(self.npm / "claude.cmd"))

    def test_exe_is_preferred_over_cmd(self) -> None:
        (self.npm / "claude.exe").write_bytes(b"MZ")
        found = hostpaths.find_executable(
            "claude", windows=True, local_app_data=str(self.local)
        )
        self.assertEqual(found, str(self.npm / "claude.exe"))

    def test_other_hosts_do_not_search_packages(self) -> None:
        self.assertIsNone(
            hostpaths.find_executable(
                "claude", windows=False, local_app_data=str(self.local)
            )
        )

    def test_missing_tool_and_missing_packages_folder(self) -> None:
        self.assertIsNone(
            hostpaths.find_executable(
                "codex", windows=True, local_app_data=str(self.local)
            )
        )
        self.assertEqual(hostpaths.packaged_npm_roots(str(self.local / "nope")), [])

    def test_path_wins_over_packages(self) -> None:
        with mock.patch.object(hostpaths.shutil, "which", return_value=r"C:\bin\claude.exe"):
            self.assertEqual(
                hostpaths.find_executable(
                    "claude", windows=True, local_app_data=str(self.local)
                ),
                r"C:\bin\claude.exe",
            )


if __name__ == "__main__":
    unittest.main()
