import tempfile
import unittest
from pathlib import Path

from mailman.environment import load_plan
from mailman.environment_plan import draft_plan


class DraftEnvironmentTests(unittest.TestCase):
    def test_declared_test_groups_and_extras_make_a_runnable_plan(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('''
[project]
name = "fixture"
requires-python = ">=3.12"
[build-system]
requires = ["setuptools"]
[project.optional-dependencies]
tests = ["pytest"]
[dependency-groups]
common = ["hypothesis"]
test = [{include-group = "common"}, "pytest"]
''', encoding="utf-8")
            path = root / "plan.json"
            draft_plan(root, path)
            plan = load_plan(path)
            self.assertIn("hypothesis", plan["steps"][1]["command"])
            self.assertEqual(plan["steps"][2]["command"][-1], ".[tests]")
            self.assertIn("--only-binary=:all:", plan["steps"][2]["command"])
            self.assertEqual(plan["draft"]["requires_python"], ">=3.12")
            with self.assertRaisesRegex(ValueError, "already exists"):
                draft_plan(root, path)

    def test_group_cycles_refuse_without_writing_a_partial_plan(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('[dependency-groups]\ntest = [{include-group = "test"}]', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "reference"):
                draft_plan(root, root / "plan.json")
            self.assertFalse((root / "plan.json").exists())
