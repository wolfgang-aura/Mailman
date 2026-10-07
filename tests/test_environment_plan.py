import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mailman.environment import load_plan
from unittest import mock

from mailman.environment_plan import _COPY_COMPILED, HOST_CONSTRAINTS_FILENAME, draft_plan


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
            self.assertIn("--prefer-binary", plan["steps"][2]["command"])
            self.assertNotIn("--only-binary=:all:", plan["steps"][1]["command"])
            self.assertEqual(plan["draft"]["requires_python"], ">=3.12")
            with self.assertRaisesRegex(ValueError, "already exists"):
                draft_plan(root, path)

    def test_extras_the_poe_test_task_installs_are_installed(self):
        # schwifty: `test = "uv run --extra pydantic pytest ..."`; the plan
        # installed `.` and test_pydantic_protocol failed at baseline. Mailman #360.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('''
[project]
name = "fixture"
[project.optional-dependencies]
pydantic = ["pydantic>=2.0"]
docs = ["sphinx"]
[tool.poe.tasks]
test = "uv run --extra pydantic pytest --cov"
''', encoding="utf-8")
            path = root / "plan.json"
            draft_plan(root, path)
            self.assertEqual(load_plan(path)["steps"][2]["command"][-1], ".[pydantic]")

    def test_all_extras_in_a_poe_test_task_join_the_test_extra(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('''
[project]
name = "fixture"
[project.optional-dependencies]
test = ["pytest"]
yaml = ["pyyaml"]
[tool.poe.tasks.test]
cmd = "uv run --all-extras pytest"
''', encoding="utf-8")
            path = root / "plan.json"
            draft_plan(root, path)
            self.assertEqual(load_plan(path)["steps"][2]["command"][-1], ".[test,yaml]")

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

    def test_a_direct_reference_dependency_gets_the_common_build_backends(self):
        """spikeinterface builds neo and probeinterface from git with hatchling (#364)."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('''
[project]
name = "fixture"
dependencies = ["numpy", "probeinterface @ git+https://github.com/SpikeInterface/probeinterface.git"]
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
''', encoding="utf-8")
            build = draft_plan(root, root / "plan.json")["steps"][1]["command"]
            for backend in ("hatchling", "hatch-vcs", "setuptools-scm", "flit-core", "poetry-core"):
                self.assertIn(backend, build)

    def test_registry_dependencies_add_no_extra_build_backends(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('''
[project]
name = "fixture"
dependencies = ["numpy>=2"]
[project.optional-dependencies]
test = ["pytest"]
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"
''', encoding="utf-8")
            build = draft_plan(root, root / "plan.json")["steps"][1]["command"]
            self.assertNotIn("hatchling", build)

    def test_hatch_test_environments_supply_test_dependencies(self):
        """edgartools declares its test tools only in hatch's default env (#133)."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('''
[project]
name = "fixture"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.envs.default]
dependencies = ["pytest-asyncio", "vcrpy<8.2", "fixture-plugin @ {root:uri}/plugin"]
[tool.hatch.envs.hatch-test]
extra-dependencies = ["freezegun"]
[tool.hatch.envs.docs]
dependencies = ["mkdocs"]
''', encoding="utf-8")
            plan = draft_plan(root, root / "plan.json")
            build = plan["steps"][1]["command"]
            for requirement in ("pytest-asyncio", "vcrpy<8.2", "freezegun", "setuptools"):
                self.assertIn(requirement, build)
            self.assertNotIn("mkdocs", build)
            self.assertFalse(any("{root:uri}" in part for part in build))
            self.assertEqual(plan["draft"]["hatch_environments"], ["default", "hatch-test"])

    def test_poetry_test_group_supplies_test_dependencies(self):
        """docformatter declares pytest only in a Poetry group (#450)."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('''
[tool.poetry]
name = "fixture"
[tool.poetry.group.dev.dependencies]
Sphinx = "^6.0.0"
[tool.poetry.group.testing.dependencies]
coverage = {extras = ["toml"], version = "^7.5.0"}
mock = "^5.2.0"
pytest = ">=8.4,<10.0"
pytest-order = "~1.3"
legacy = "0.4.2"
local = {path = "../local"}
[build-system]
requires = ["poetry-core>=1.0.0"]
build-backend = "poetry.core.masonry.api"
''', encoding="utf-8")
            plan = draft_plan(root, root / "plan.json")
            build = plan["steps"][1]["command"]
            for requirement in ("coverage[toml]>=7.5.0,<8", "mock>=5.2.0,<6", "pytest>=8.4,<10.0",
                                "pytest-order>=1.3,<1.4", "legacy==0.4.2"):
                self.assertIn(requirement, build)
            self.assertFalse(any(part.startswith(("Sphinx", "local")) for part in build))
            self.assertEqual(plan["draft"]["group"], "tool.poetry.group.testing")

    def test_sdist_only_dependencies_are_not_refused(self):
        """beets' langdetect publishes no wheel and is pure Python (#133)."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "fixture"\n[dependency-groups]\ntest = ["langdetect"]\n',
                encoding="utf-8",
            )
            plan = draft_plan(root, root / "plan.json")
            for step in plan["steps"][1:]:
                self.assertIn("--prefer-binary", step["command"])
                self.assertNotIn("--only-binary=:all:", step["command"])


    def test_windows_plans_steer_pip_off_releases_application_control_blocks(self):
        """nilearn#6607 installed scikit-learn 1.9.1, whose DLLs are blocked (#146)."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "fixture"\n[dependency-groups]\ntest = ["pytest"]\n',
                encoding="utf-8",
            )
            with mock.patch("mailman.environment_plan.sys.platform", "win32"):
                plan = draft_plan(root, root / "run" / "plan.json")
            constraint_file = (root / "run" / HOST_CONSTRAINTS_FILENAME).resolve()
            constraints = constraint_file.read_text(encoding="utf-8").splitlines()
            self.assertIn("scikit-learn!=1.9.1", constraints)
            # PyPSA#1938: pyproj 3.8.0's `_network` DLL is blocked. #197.
            self.assertIn("pyproj!=3.8.0", constraints)
            # awkward#4228: pyarrow 25.0.1's `_fs` DLL is blocked. #369.
            self.assertIn("pyarrow!=25.0.1", constraints)
            # uproot5#1529: cramjam 2.13.0's DLL is blocked. #374.
            self.assertIn("cramjam!=2.13.0", constraints)
            # mpmath#1158: hypothesis 6.168.5's `_native` DLL is blocked. #427.
            self.assertIn("hypothesis!=6.168.5", constraints)
            # pyvista#8104: vtk 9.7.1's DLLs are blocked. #429.
            self.assertIn("vtk!=9.7.1", constraints)
            # pyvista#8104: pyvista-zstd's `pvzstd.dll` is blocked. #430.
            self.assertIn("pyvista-zstd!=0.4.2,!=0.5.0", constraints)
            for step in plan["steps"][1:]:
                command = step["command"]
                self.assertEqual(
                    command[command.index("-c") + 1], str(constraint_file)
                )
            # A constraint installs nothing the target did not ask for.
            self.assertNotIn("scikit-learn!=1.9.1", plan["steps"][1]["command"])

    def test_uv_lock_versions_become_constraints(self):
        """bedrock-agentcore locks strands-agents 1.56.0; 1.58.1 failed its baseline (#447)."""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "fixture"\n[dependency-groups]\ndev = ["strands-agents>=1.56.0"]\n',
                encoding="utf-8",
            )
            (root / "uv.lock").write_text(
                'version = 1\n'
                '[[package]]\nname = "Strands_Agents"\nversion = "1.56.0"\nsource = { registry = "https://pypi.org/simple" }\n'
                '[[package]]\nname = "fixture"\nversion = "0.1.0"\nsource = { editable = "." }\n'
                '[[package]]\nname = "numpy"\nversion = "2.2.6"\nsource = { registry = "https://pypi.org/simple" }\n'
                '[[package]]\nname = "numpy"\nversion = "2.3.4"\nsource = { registry = "https://pypi.org/simple" }\n'
                '[[package]]\nname = "pandas"\nversion = "3.0.6"\nsource = { registry = "https://pypi.org/simple" }\n',
                encoding="utf-8",
            )
            with mock.patch("mailman.environment_plan.sys.platform", "win32"):
                plan = draft_plan(root, root / "run" / "plan.json")
            constraints = (root / "run" / HOST_CONSTRAINTS_FILENAME).read_text(encoding="utf-8").splitlines()
            self.assertIn("strands-agents==1.56.0", constraints)
            # The project itself, a platform fork and a host-blocked release stay unpinned.
            self.assertFalse(any(line.startswith(("fixture", "numpy", "pandas==")) for line in constraints))
            self.assertIn("pandas!=3.0.6", constraints)
            self.assertIn("Pinned 1 package(s)", plan["draft"]["review"])

    def test_a_c_extension_target_on_windows_overlays_the_release_wheel(self):
        # biopython: `pip install -e .` compiles C and this host has no
        # compiler. The release wheel's compiled modules go into the workspace
        # and the workspace goes on sys.path. Mailman #213.
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "biopython"\ndependencies = ["numpy"]\n'
                '[dependency-groups]\ntest = ["pytest"]\n',
                encoding="utf-8",
            )
            (root / "setup.py").write_text(
                "from setuptools import Extension, setup\n"
                "setup(ext_modules=[Extension('Bio.Align._aligncore', ['x.c'])])\n",
                encoding="utf-8",
            )
            with mock.patch("mailman.environment_plan.sys.platform", "win32"):
                plan = draft_plan(root, root / "run" / "plan.json")
            names = [step["name"] for step in plan["steps"]]
            flat = [" ".join(step["command"]) for step in plan["steps"]]
            self.assertNotIn("install-target", names)
            self.assertIn("numpy", plan["steps"][1]["command"])
            self.assertTrue(any("--only-binary :all: --no-deps biopython" in c for c in flat))
            self.assertTrue(any("uninstall -y biopython" in c for c in flat))
            self.assertTrue(any(".pth" in c for c in flat))
            self.assertIn("compiled", plan["draft"]["review"])
            self.assertTrue(plan["draft"]["compiled_extensions"])

    def test_a_c_extension_target_off_windows_keeps_the_editable_install(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
            (root / "setup.py").write_text("ext_modules=[Extension('x', ['x.c'])]\n", encoding="utf-8")
            with mock.patch("mailman.environment_plan.sys.platform", "linux"):
                plan = draft_plan(root, root / "run" / "plan.json")
            self.assertEqual(plan["steps"][-1]["name"], "install-target")


class CopyCompiledTests(unittest.TestCase):
    def test_a_pure_release_wheel_copies_nothing_and_succeeds(self) -> None:
        # Pyomo ships py3-none-any: its extension is optional, the copy found
        # no module and failed the plan the screen had cleared. Mailman #392.
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-c", _COPY_COMPILED, "pytest"],
                cwd=temporary, capture_output=True, text=True, timeout=60,
            )
            copied = list(Path(temporary).rglob("*"))

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(copied, [])
