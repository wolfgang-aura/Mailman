from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from mailman.artifacts import create_run
from mailman.toolchain import prepare_agent_prompt, probe_tool


class ToolchainTests(unittest.TestCase):
    def test_probe_records_executable_and_prompt_uses_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/1",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                data_root=root / "runs",
            )
            result = probe_tool(
                run_directory,
                name="python",
                executable=Path(sys.executable),
                probe_arguments=["--version"],
                timeout_seconds=5,
            )
            source = root / "source-prompt.md"
            source.write_text("Fix the issue.\n", encoding="utf-8")
            prepared = prepare_agent_prompt(
                run_directory, role="primary", source_prompt=source
            )

            self.assertEqual(result.exit_code, 0)
            manifest = json.loads(
                (run_directory / "toolchain.json").read_text(encoding="utf-8")
            )
            executable = str(Path(sys.executable).resolve())
            self.assertEqual(manifest["tools"]["python"]["executable"], executable)
            expected_hash = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
            self.assertEqual(manifest["tools"]["python"]["sha256"], expected_hash)
            prompt = prepared.read_text(encoding="utf-8")
            self.assertIn("Mailman verified toolchain", prompt)
            self.assertIn(executable, prompt)
            self.assertIn(expected_hash, prompt)

    def test_prompt_rejects_executable_changed_after_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            _, run_directory = create_run(
                repository="https://github.com/example/project.git",
                issue="https://github.com/example/project/issues/1",
                base_commit="a" * 40,
                primary="codex",
                reviewer="claude",
                data_root=root / "runs",
            )
            probe_tool(
                run_directory,
                name="python",
                executable=Path(sys.executable),
                probe_arguments=["--version"],
                timeout_seconds=5,
            )
            manifest_path = run_directory / "toolchain.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["tools"]["python"]["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            source = root / "source-prompt.md"
            source.write_text("Fix the issue.\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "changed after probe"):
                prepare_agent_prompt(
                    run_directory, role="primary", source_prompt=source
                )

    def test_probe_rejects_invalid_tool_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            run_directory = Path(temporary_directory)
            with self.assertRaisesRegex(ValueError, "tool name"):
                probe_tool(
                    run_directory,
                    name="Python Runtime",
                    executable=Path(sys.executable),
                    probe_arguments=["--version"],
                    timeout_seconds=5,
                )


if __name__ == "__main__":
    unittest.main()
