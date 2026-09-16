"""The line the issue quotes is the cheapest way to ask whether it still exists.

`prescreen` passed vyperlang/vyper#5113 and #5136, and both were already fixed
at the base commit by a merged pull request that never cited the issue, so no
duplicate search and no prior-art read could see it. Both bodies quote the exact
source line they complain about.
https://github.com/wolfgang-aura/Mailman/issues/103
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from mailman.base_snippets import (
    check_base_snippets,
    issue_snippets,
    looks_like_source,
)
from mailman.targeting import ALREADY_FIXED_UPSTREAM, assess_target

#: vyperlang/vyper#5113 as `mailman fetch-issue` captured it on 2026-09-16, cut
#: to the paragraphs that carry evidence. The run dropped after
#: `prepare-workspace`; base `6f1aefb5` already contained vyper#5227, which
#: replaced this `CompilerPanic` with a `BundleError`.
VYPER_5113 = '''\
# vyperlang/vyper#5113: Bundle output format panics on out-of-tree path

- Source: https://github.com/vyperlang/vyper/issues/5113
- Capture method: github-cli

## Issue body

Compiling a **valid** contract with one of the source-bundling output formats
(`-f solc_json`, `-f archive`) raises an internal `CompilerPanic` when the input
file is referenced by a path that is not under the current directory.

Command and error (run from a directory that does not contain the input file):

```
$ cd /workspace
$ vyper -f solc_json /tmp/s.vy
vyper.exceptions.CompilerPanic: Invalid path: /tmp/s.vy
```

Observed from cwd `/workspace`:

```
$ vyper -f solc_json   /tmp/s.vy        # CompilerPanic: Invalid path: /tmp/s.vy
$ vyper -f abi         /tmp/s.vy        # OK (non-bundle formats are unaffected)
$ vyper -f solc_json -p /tmp /tmp/s.vy  # OK (adding the dir as a search path fixes it)
```

The relevant code even acknowledges this can be a bug:

```python
# vyper/compiler/output_bundle.py:120-121
            # this shouldn't happen unless a file escapes its package,
            # *or* if we have a bug
            if not ok:
                raise CompilerPanic(f"Invalid path: {c.resolved_path}")
```

### How can it be fixed?

Replace the `CompilerPanic` with a clean user-facing error.

## Capture boundary

This file is the only issue text the agents see.
'''

#: vyperlang/vyper#5136, the same hunt. Its quoted lines are the *legacy*
#: behaviour the reporter wants copied, not the broken line, so they are all
#: still at base and this check correctly says nothing.
VYPER_5136 = '''\
# vyperlang/vyper#5136: Venom pipeline drops static assert-false compile-time check

- Source: https://github.com/vyperlang/vyper/issues/5136
- Capture method: github-cli

## Issue body

The venom pipeline silently compiles `assert False` to a runtime-reverting
contract, whereas the legacy IR pipeline rejects the same source at compile time.

Source-level evidence of the asymmetry:

```python
# vyper/ir/optimizer.py:551
if value == 0:
    raise StaticAssertionException(
        f"assertion found to fail at compile time. ..."
    )
```

```python
# vyper/venom/passes/assert_elimination.py:24-26
rng = variable_ranges.get_range(operand, inst)
if self._range_excludes_zero(rng):
    inst.make_nop()
```

## Capture boundary

This file is the only issue text the agents see.
'''


def _git(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return completed.stdout.strip()


class SnippetReadingTests(unittest.TestCase):
    def test_the_quoted_panic_is_read_with_the_file_it_names(self) -> None:
        snippets = issue_snippets(VYPER_5113)

        self.assertEqual(len(snippets), 1)
        self.assertEqual(
            snippets[0]["text"],
            'raise CompilerPanic(f"Invalid path: {c.resolved_path}")',
        )
        self.assertEqual(snippets[0]["path"], "vyper/compiler/output_bundle.py")
        self.assertEqual(snippets[0]["line"], 120)

    def test_the_command_transcript_is_not_read_as_source(self) -> None:
        # `$ vyper -f solc_json -p /tmp /tmp/s.vy  # OK (adding the dir ...)`
        # carries a parenthesised aside that reads as a call. A shell prompt
        # settles it before the call pattern is ever tried.
        for line in VYPER_5113.splitlines():
            if line.startswith("$ "):
                self.assertFalse(looks_like_source(line), line)
        self.assertNotIn(
            "solc_json", " ".join(row["text"] for row in issue_snippets(VYPER_5113))
        )

    def test_every_quoted_line_keeps_the_file_its_fence_named(self) -> None:
        snippets = issue_snippets(VYPER_5136)
        by_path: dict[str, list[str]] = {}
        for row in snippets:
            by_path.setdefault(row["path"], []).append(row["text"])

        self.assertEqual(
            by_path["vyper/ir/optimizer.py"], ["raise StaticAssertionException("]
        )
        self.assertEqual(
            by_path["vyper/venom/passes/assert_elimination.py"],
            [
                "rng = variable_ranges.get_range(operand, inst)",
                "if self._range_excludes_zero(rng):",
                "inst.make_nop()",
            ],
        )

    def test_prose_and_short_fragments_are_not_snippets(self) -> None:
        body = (
            "## Issue body\n\nThe parser is wrong (I think) and the docs "
            "disagree.\n\n```python\n# pkg/thing.py:4\nif x:\n)\n```\n"
        )
        self.assertEqual(issue_snippets(body), [])

    def test_the_snippet_count_is_bounded(self) -> None:
        lines = "\n".join(f"raise Error{index}(value, other)" for index in range(40))
        body = f"## Issue body\n\n```python\n# pkg/thing.py:1\n{lines}\n```\n"
        self.assertEqual(len(issue_snippets(body)), 20)


class BaseTreeTests(unittest.TestCase):
    def _run_directory(self, issue: str, tree: dict[str, str]) -> tuple[Path, str]:
        """A run directory whose workspace is a one-commit repository."""
        root = Path(self.temporary.name)
        workspace = root / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        _git(workspace, "init", "--quiet")
        _git(workspace, "config", "user.email", "test@example.com")
        _git(workspace, "config", "user.name", "Test")
        for path, text in tree.items():
            destination = workspace / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8")
        _git(workspace, "add", "--all")
        _git(workspace, "commit", "--quiet", "-m", "base")
        (root / "issue.md").write_text(issue, encoding="utf-8")
        return root, _git(workspace, "rev-parse", "HEAD")

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def test_a_quoted_line_upstream_removed_reports_already_fixed(self) -> None:
        # vyper#5227 replaced the panic with a BundleError before this base
        # commit, and never cited #5113.
        root, base = self._run_directory(
            VYPER_5113,
            {
                "vyper/compiler/output_bundle.py": (
                    "def _to_pathlib(c):\n"
                    "    if not ok:\n"
                    '        raise BundleError(f"Invalid path: '
                    '{c.resolved_path}")\n'
                )
            },
        )

        record = check_base_snippets(root, base_commit=base)

        self.assertTrue(record["success"])
        self.assertTrue(record["already_fixed"])
        self.assertEqual(
            record["decided_by"]["path"], "vyper/compiler/output_bundle.py"
        )
        self.assertIn("CompilerPanic", record["decided_by"]["text"])
        self.assertIn("already fixed at base", record["detail"])
        self.assertIn(base, record["detail"])

    def test_check_target_refuses_the_run_on_the_snippet_alone(self) -> None:
        root, base = self._run_directory(
            VYPER_5113,
            {"vyper/compiler/output_bundle.py": "def _to_pathlib(c):\n    pass\n"},
        )
        check_base_snippets(root, base_commit=base)

        assessment = assess_target(root)

        self.assertFalse(assessment.may_start)
        self.assertIn(ALREADY_FIXED_UPSTREAM, assessment.blocking)
        self.assertEqual(assessment.blocking.count(ALREADY_FIXED_UPSTREAM), 1)
        self.assertIn("output_bundle.py", assessment.summary())

    def test_a_quoted_line_still_at_base_decides_nothing(self) -> None:
        # vyper#5136 quotes the behaviour it wants copied, not the broken line.
        # Every snippet is still there, so the check must stay silent.
        root, base = self._run_directory(
            VYPER_5136,
            {
                "vyper/ir/optimizer.py": (
                    "if value == 0:\n"
                    "    raise StaticAssertionException(\n"
                    '        f"assertion found to fail at compile time."\n'
                    "    )\n"
                ),
                "vyper/venom/passes/assert_elimination.py": (
                    "def run_pass(self, inst, operand, variable_ranges):\n"
                    "    rng = variable_ranges.get_range(operand, inst)\n"
                    "    if self._range_excludes_zero(rng):\n"
                    "        inst.make_nop()\n"
                ),
            },
        )

        record = check_base_snippets(root, base_commit=base)

        self.assertTrue(record["success"])
        self.assertFalse(record["already_fixed"])
        self.assertEqual(record["findings"], [])
        self.assertIn("none of them missing", record["detail"])
        self.assertNotIn(ALREADY_FIXED_UPSTREAM, assess_target(root).blocking)

    def test_a_line_that_moved_to_another_file_is_still_present(self) -> None:
        # The search is tree-wide on purpose. A line refactored into another
        # module has not been fixed, and dropping that target would be wrong.
        root, base = self._run_directory(
            VYPER_5113,
            {
                "vyper/compiler/output_bundle.py": "from vyper.compiler import _paths\n",
                "vyper/compiler/_paths.py": (
                    '        raise CompilerPanic(f"Invalid path: '
                    '{c.resolved_path}")\n'
                ),
            },
        )

        record = check_base_snippets(root, base_commit=base)

        self.assertFalse(record["already_fixed"])
        self.assertTrue(record["snippets"][0]["present"])

    def test_reformatting_alone_does_not_read_as_a_fix(self) -> None:
        # A literal search calls a rewrapped line absent. The named file is
        # read again with whitespace collapsed before anything is decided.
        root, base = self._run_directory(
            VYPER_5113,
            {
                "vyper/compiler/output_bundle.py": (
                    "    raise CompilerPanic(\n"
                    '        f"Invalid path: {c.resolved_path}"\n'
                    "    )\n"
                )
            },
        )

        record = check_base_snippets(root, base_commit=base)

        self.assertFalse(record["already_fixed"])
        self.assertTrue(record["snippets"][0]["matched_ignoring_whitespace"])

    def test_a_file_upstream_deleted_explains_the_absence(self) -> None:
        # The quoted line is gone and so is its file. That is a rename or a
        # removal, not evidence that this issue was fixed.
        root, base = self._run_directory(VYPER_5113, {"README.md": "nothing here\n"})

        record = check_base_snippets(root, base_commit=base)

        self.assertFalse(record["already_fixed"])
        self.assertFalse(record["snippets"][0]["file_exists"])

    def test_no_workspace_records_the_refusal_rather_than_a_verdict(self) -> None:
        root = Path(self.temporary.name)
        (root / "issue.md").write_text(VYPER_5113, encoding="utf-8")

        record = check_base_snippets(root, base_commit="a" * 40)

        self.assertFalse(record["success"])
        self.assertFalse(record["already_fixed"])
        self.assertIn("no prepared workspace", record["detail"])
        self.assertNotIn(ALREADY_FIXED_UPSTREAM, assess_target(root).blocking)


if __name__ == "__main__":
    unittest.main()
