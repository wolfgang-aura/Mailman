from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from mailman.instructions import describe_instruction_sources


class InstructionSourceTests(unittest.TestCase):
    def _home(self, root: Path) -> Path:
        home = root / "home"
        (home / ".codex" / "skills" / "unslop").mkdir(parents=True)
        (home / ".claude").mkdir(parents=True)
        (home / ".codex" / "AGENTS.md").write_text("be terse", encoding="utf-8")
        (home / ".codex" / "config.toml").write_text("model = 'x'", encoding="utf-8")
        (home / ".codex" / "skills" / "unslop" / "SKILL.md").write_text(
            "cut the slop", encoding="utf-8"
        )
        return home

    def test_records_the_personal_files_a_codex_run_still_reads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = self._home(Path(temporary_directory))

            sources = describe_instruction_sources("codex", home=home)

            by_path = {source["path"]: source for source in sources}
            agents = by_path[str(home / ".codex" / "AGENTS.md")]
            self.assertTrue(agents["present"])
            self.assertFalse(agents["suppressed"])
            self.assertEqual(
                agents["sha256"], hashlib.sha256(b"be terse").hexdigest()
            )
            # `--ignore-user-config` covers this one and nothing else.
            self.assertTrue(
                by_path[str(home / ".codex" / "config.toml")]["suppressed"]
            )
            skill = by_path[str(home / ".codex" / "skills" / "unslop" / "SKILL.md")]
            self.assertEqual(skill["kind"], "skill")
            self.assertEqual(skill["name"], "unslop")

    def test_records_a_missing_file_as_absent_rather_than_omitting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "empty-home"
            home.mkdir()

            sources = describe_instruction_sources("claude", home=home)

            self.assertTrue(sources)
            self.assertTrue(all(source["present"] is False for source in sources))
            self.assertIn(
                str(home / ".claude" / "CLAUDE.md"),
                [source["path"] for source in sources],
            )


if __name__ == "__main__":
    unittest.main()
