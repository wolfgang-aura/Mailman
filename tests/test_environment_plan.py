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

    def test_a_hatchling_target_installs_editables_for_the_editable_build(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('''
[project]
name = "fixture"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[dependency-groups]
test = ["pytest"]
''', encoding="utf-8")
            path = root / "plan.json"
            plan = draft_plan(root, path)
            build = plan["steps"][1]["command"]
            install = plan["steps"][2]["command"]
            self.assertIn("--no-build-isolation", install)
            self.assertIn("-e", install)
            self.assertIn("editables", build)
            self.assertEqual(build.count("editables"), 1)

    def test_a_declared_setuptools_backend_does_not_install_editables(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('''
[project]
name = "fixture"
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
[dependency-groups]
test = ["pytest"]
''', encoding="utf-8")
            plan = draft_plan(root, root / "plan.json")
            self.assertNotIn("editables", plan["steps"][1]["command"])

    def test_an_undeclared_backend_installs_editables_because_it_may_be_hatchling(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('[project]\nname = "fixture"\n', encoding="utf-8")
            plan = draft_plan(root, root / "plan.json")
            self.assertIn("editables", plan["steps"][1]["command"])
