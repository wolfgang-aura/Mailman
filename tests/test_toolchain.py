from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from mailman.artifacts import create_run
from mailman.toolchain import (
    prepare_agent_prompt,
    probe_tool,
    resolve_command,
    toolchain_executable,
)


class ToolchainExecutableTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        _, self.run_directory = create_run(
            repository="https://github.com/example/project.git",
            issue="https://github.com/example/project/issues/1",
            base_commit="a" * 40,
            primary="codex",
            reviewer="claude",
            data_root=self.root / "runs",
        )

    def test_an_unprobed_tool_has_no_pinned_executable(self) -> None:
        self.assertIsNone(toolchain_executable(self.run_directory, "codex"))

    def test_a_probed_tool_returns_its_exact_executable(self) -> None:
        probe_tool(
            self.run_directory,
            name="python",
            executable=Path(sys.executable),
            probe_arguments=["--version"],
            timeout_seconds=5,
        )
        self.assertEqual(
            toolchain_executable(self.run_directory, "python"),
            str(Path(sys.executable).resolve()),
        )

    def test_a_changed_executable_is_rejected(self) -> None:
        probe_tool(
            self.run_directory,
            name="python",
            executable=Path(sys.executable),
            probe_arguments=["--version"],
            timeout_seconds=5,
        )
        manifest_path = self.run_directory / "toolchain.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["tools"]["python"]["sha256"] = hashlib.sha256(b"other").hexdigest()
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed after probe"):
            toolchain_executable(self.run_directory, "python")


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


class ResolveCommandTests(unittest.TestCase):
    """A bare executable name in a verification command names the run's tool.

    Before this, `python -m pytest ...` ran whatever interpreter was first on
    PATH. In run 20260903T050831Z-bed67e that was the host interpreter, which
    has none of the target's dependencies, so Mailman's own gate failed on
    ModuleNotFoundError rather than on the candidate.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        _, self.run_directory = create_run(
            repository="https://github.com/example/project.git",
            issue="https://github.com/example/project/issues/1",
            base_commit="a" * 40,
            primary="claude",
            reviewer="claude",
            data_root=self.root / "runs",
        )

    def test_a_bare_name_resolves_to_the_registered_executable(self) -> None:
        probe_tool(
            self.run_directory,
            name="python",
            executable=Path(sys.executable),
            probe_arguments=["--version"],
            timeout_seconds=30,
        )
        self.assertEqual(
            resolve_command(self.run_directory, ["python", "-m", "pytest", "-q"]),
            [str(Path(sys.executable).resolve()), "-m", "pytest", "-q"],
        )

    def test_an_executable_written_as_a_path_is_left_alone(self) -> None:
        probe_tool(
            self.run_directory,
            name="python",
            executable=Path(sys.executable),
            probe_arguments=["--version"],
            timeout_seconds=30,
        )
        written = str(self.run_directory / "environment" / "Scripts" / "python.exe")
        self.assertEqual(
            resolve_command(self.run_directory, [written, "-m", "pytest"]),
            [written, "-m", "pytest"],
        )

    def test_an_unregistered_bare_name_falls_back_to_path(self) -> None:
        resolved = resolve_command(self.run_directory, ["python", "--version"])
        self.assertTrue(Path(resolved[0]).is_file())
        self.assertEqual(resolved[1:], ["--version"])

    def test_an_empty_command_is_returned_unchanged(self) -> None:
        self.assertEqual(resolve_command(self.run_directory, []), [])
