"""The repository screen, gate by gate.

Every fixture here is a repository that actually failed a hand screen on
2026-09-03: OpenBB on freshness, freqtrade on a single recurring collaborator,
ccxt on generated Python, hummingbot on a compiler. See
https://github.com/wolfgang-aura/Mailman/issues/35.
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mailman.screen import (
    load_screen,
    render_screen,
    screen_repository,
)


def _days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _pull(number: int, *, author: str, merged_days_ago: int | None) -> dict:
    return {
        "number": number,
        "title": f"fix {number}",
        "user": {"login": author, "type": "User"},
        "author_association": "CONTRIBUTOR",
        "merged_at": None if merged_days_ago is None else _days_ago(merged_days_ago),
        "closed_at": _days_ago(merged_days_ago or 1),
        "state": "closed",
        "head": {"ref": f"fix-{number}"},
        "body": "",
    }


def _issue(number: int, *, days_old: int = 3, assignee: object = None) -> dict:
    return {
        "number": number,
        "title": f"bug {number}",
        "created_at": _days_ago(days_old),
        "assignee": assignee,
        "labels": [],
        "comments": 0,
    }


def _contents(text: str) -> dict:
    return {
        "encoding": "base64",
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }


class _Result:
    def __init__(self, stdout: str, exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code
        self.timed_out = False

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code}


HEALTHY_WORKFLOW = "jobs:\n  test:\n    steps:\n      - run: pytest -q\n"
PUBLISH_WORKFLOW = "jobs:\n  publish:\n    steps:\n      - run: twine upload dist/*\n"


class FakeGitHub:
    """Answer the paths the screen asks for, from a dict of canned payloads."""

    def __init__(self, **overrides) -> None:
        self.closed_pulls = overrides.pop(
            "closed_pulls",
            [
                _pull(1, author="alice", merged_days_ago=2),
                _pull(2, author="bob", merged_days_ago=20),
            ],
        )
        self.open_pulls = overrides.pop("open_pulls", [])
        self.issues = overrides.pop("issues", [_issue(10), _issue(11)])
        self.languages = overrides.pop("languages", {"Python": 100000})
        self.workflows = overrides.pop(
            "workflows", {"ci.yml": HEALTHY_WORKFLOW}
        )
        self.root = overrides.pop("root", [{"name": "pyproject.toml"}])
        self.policies = overrides.pop("policies", {})
        self.meta = overrides.pop(
            "meta",
            {
                "full_name": "example/project",
                "stargazers_count": 4200,
                "default_branch": "main",
                "archived": False,
                "created_at": _days_ago(1500),
                "fork": False,
            },
        )
        self.missing_workflows = overrides.pop("missing_workflows", False)
        assert not overrides, f"unexpected fixture keys: {sorted(overrides)}"
        self.asked: list[str] = []

    def __call__(self, arguments, **keywords):
        path = arguments[-1]
        self.asked.append(path)
        return _Result(json.dumps(self._payload(path)))

    def _payload(self, path: str):
        base = path.split("?", 1)[0]
        if base.endswith("/languages"):
            return self.languages
        if "/contents/.github/workflows/" in base:
            name = base.rsplit("/", 1)[-1]
            return _contents(self.workflows.get(name, ""))
        if base.endswith("/contents/.github/workflows"):
            if self.missing_workflows:
                return {"message": "Not Found"}
            return [{"name": name} for name in self.workflows]
        if "/contents/" in base:
            relative = base.split("/contents/", 1)[1]
            if relative in self.policies:
                return _contents(self.policies[relative])
            return {"message": "Not Found"}
        if base.endswith("/contents"):
            return self.root
        if "/pulls" in base:
            rows = self.open_pulls if "state=open" in path else self.closed_pulls
            return rows if "page=1" in path or "page=" not in path else []
        if "/issues" in base:
            return self.issues if "page=1" in path or "page=" not in path else []
        return self.meta


def _named(record: dict, name: str) -> dict:
    """Look a gate up by name. Positions shift whenever a gate is added."""
    return next(gate for gate in record["gates"] if gate["name"] == name)


def _screen(root: Path, gh: FakeGitHub, **keywords) -> dict:
    return screen_repository(
        "https://github.com/example/project.git",
        data_root=root,
        executable="gh",
        working_directory=root,
        _execute=gh,
        **keywords,
    )


class ScreenTests(unittest.TestCase):
    def test_a_healthy_repository_passes_every_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = _screen(root, FakeGitHub())

            self.assertTrue(record["success"])
            self.assertEqual(record["verdict"], "pass")
            self.assertEqual(record["failed_gates"], [])
            self.assertEqual(load_screen(root, "example/project"), record)
        self.assertIn("worth a run", render_screen(record))

    def test_a_repository_with_no_recent_outside_merge_fails_first(self) -> None:
        # OpenBB-finance/OpenBB: 72.6k stars, last outside merge six weeks back.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    closed_pulls=[
                        _pull(1, author="alice", merged_days_ago=42),
                        _pull(2, author="bob", merged_days_ago=50),
                    ]
                ),
            )

        self.assertEqual(record["verdict"], "fail")
        self.assertIn("freshness", record["failed_gates"])
        freshness = _named(record, "freshness")
        self.assertEqual(freshness["data"]["merges_in_window"], 0)
        self.assertIn("no outside human merge", freshness["detail"])

    def test_one_recurring_collaborator_is_not_an_open_door(self) -> None:
        # freqtrade/freqtrade merged yesterday, and every outside merge for
        # three years belongs to the same person.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    closed_pulls=[
                        _pull(1, author="solo", merged_days_ago=1),
                        _pull(2, author="solo", merged_days_ago=9),
                        _pull(3, author="solo", merged_days_ago=30),
                    ]
                ),
            )
        freshness = _named(record, "freshness")

        self.assertEqual(record["verdict"], "fail")
        self.assertIn("freshness", record["failed_gates"])
        self.assertEqual(freshness["data"]["distinct_outside_authors"], 1)
        self.assertEqual(freshness["data"]["top_author"], "solo")
        self.assertIn("recurring collaborator", freshness["detail"])

    def test_one_dominant_author_with_a_trickle_still_fails(self) -> None:
        # A second name appearing once does not make a repository open.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    closed_pulls=[
                        _pull(n, author="solo", merged_days_ago=n)
                        for n in range(1, 10)
                    ]
                    + [_pull(99, author="visitor", merged_days_ago=40)]
                ),
            )
        freshness = _named(record, "freshness")

        self.assertIn("freshness", record["failed_gates"])
        self.assertEqual(freshness["data"]["distinct_outside_authors"], 2)
        self.assertGreaterEqual(freshness["data"]["top_author_share"], 0.8)
        self.assertIn("trickle", freshness["detail"])

    def test_a_window_carried_by_one_frequent_author_fails(self) -> None:
        # freqtrade/freqtrade on 2026-09-04: three merges inside fourteen days,
        # all by stash86, who wrote 48% of the twenty-five outside merges in
        # ninety days. Thirteen distinct authors over the longer window made it
        # pass. https://github.com/wolfgang-aura/Mailman/issues/42
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    closed_pulls=[
                        _pull(1, author="stash86", merged_days_ago=1),
                        _pull(2, author="stash86", merged_days_ago=2),
                        _pull(3, author="stash86", merged_days_ago=3),
                    ]
                    + [
                        _pull(10 + n, author="stash86", merged_days_ago=20 + n)
                        for n in range(3)
                    ]
                    + [
                        _pull(20 + n, author=f"visitor{n}", merged_days_ago=30 + n)
                        for n in range(6)
                    ]
                ),
            )
        freshness = _named(record, "freshness")

        self.assertIn("freshness", record["failed_gates"])
        self.assertEqual(freshness["data"]["distinct_outside_authors"], 7)
        self.assertEqual(freshness["data"]["authors_in_window"], ["stash86"])
        self.assertIn("every one of the 3 merge(s)", freshness["detail"])
        self.assertIn("stash86", freshness["detail"])

    def test_a_single_window_author_on_a_small_sample_still_passes(self) -> None:
        # A share over two merges is arithmetic, not evidence about the project.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), FakeGitHub())

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(_named(record, "freshness")["data"]["authors_in_window"], ["alice"])

    def test_a_pass_names_the_authors_it_counted_and_the_bots_it_did_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    closed_pulls=[
                        _pull(1, author="alice", merged_days_ago=2),
                        _pull(2, author="bob", merged_days_ago=3),
                        _pull(3, author="dependabot[bot]", merged_days_ago=1),
                        _pull(4, author="freqtrade-bot", merged_days_ago=1),
                    ]
                ),
            )
        freshness = _named(record, "freshness")

        self.assertEqual(freshness["data"]["authors_in_window"], ["alice", "bob"])
        self.assertEqual(
            freshness["data"]["excluded_bot_authors"],
            ["dependabot[bot]", "freqtrade-bot"],
        )
        self.assertIn("alice, bob", freshness["detail"])
        self.assertIn("excluded dependabot[bot], freqtrade-bot", freshness["detail"])

    def test_a_broadly_shared_repository_passes_despite_a_leading_author(self) -> None:
        # freqtrade sits near 0.44 with fourteen authors and is genuinely open.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    closed_pulls=[
                        _pull(1, author="lead", merged_days_ago=1),
                        _pull(2, author="lead", merged_days_ago=5),
                        _pull(3, author="second", merged_days_ago=20),
                        _pull(4, author="third", merged_days_ago=30),
                        _pull(5, author="fourth", merged_days_ago=40),
                    ]
                ),
            )
        freshness = _named(record, "freshness")

        self.assertNotIn("freshness", record["failed_gates"])
        self.assertEqual(freshness["data"]["distinct_authors_in_window"], 1)
        self.assertIn("by 1 author(s)", freshness["detail"])

    def test_a_custom_test_script_counts_as_running_tests(self) -> None:
        # ccxt/ccxt runs its Python suite as `npm run test-base-rest-py`, which
        # the first version of this gate read as no tests at all.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    workflows={
                        "python.yml": (
                            "jobs:\n  build:\n    steps:\n"
                            "    - name: Run Base Tests\n"
                            "      run: npm run test-base-rest-py\n"
                        )
                    }
                ),
            )

        self.assertNotIn("ci", record["failed_gates"])
        self.assertIn("python.yml", _named(record, "ci")["data"]["workflows_running_tests"])

    def test_a_workflow_that_only_publishes_fails_the_ci_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(workflows={"release.yml": PUBLISH_WORKFLOW}),
            )

        self.assertIn("ci", record["failed_gates"])
        self.assertIn("none of them runs a test suite", _named(record, "ci")["detail"])

    def test_no_workflows_at_all_fails_the_ci_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary), FakeGitHub(workflows={}, missing_workflows=True)
            )

        self.assertIn("ci", record["failed_gates"])

    def test_generated_python_fails_the_language_gate(self) -> None:
        # ccxt/ccxt: the Python is generated from TypeScript, so a patch to it
        # is thrown away by the next build.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(languages={"TypeScript": 900000, "Python": 100000}),
            )
        gate = _named(record, "pure-python")

        self.assertIn("pure-python", record["failed_gates"])
        self.assertIn("TypeScript is the majority", gate["detail"])
        self.assertEqual(gate["data"]["python_share"], 0.1)

    def test_a_compiled_extension_fails_the_language_gate(self) -> None:
        # hummingbot/hummingbot is a Cython core, and this host has no compiler.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(languages={"Python": 700000, "Cython": 400000}),
            )

        self.assertIn("pure-python", record["failed_gates"])
        self.assertIn("compiler is in the build", _named(record, "pure-python")["detail"])

    def test_a_rust_workspace_fails_the_language_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    languages={"Python": 800000, "Rust": 300000},
                    root=[{"name": "pyproject.toml"}, {"name": "Cargo.toml"}],
                ),
            )

        self.assertIn("pure-python", record["failed_gates"])
        self.assertIn("Cargo.toml", _named(record, "pure-python")["data"]["root_markers"])

    def test_a_cython_build_back_end_fails_even_at_100_percent_python(self) -> None:
        # pmorissette/bt is every-file-a-.py and compiles bt/core.py through a
        # build hook. https://github.com/wolfgang-aura/Mailman/issues/44
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    languages={"Python": 100000},
                    # `policies` answers any /contents/<path> lookup.
                    policies={
                        "pyproject.toml": (
                            "[build-system]\n"
                            'requires = ["hatchling", "Cython>=0.29.25"]\n'
                            'build-backend = "hatchling.build"\n'
                        )
                    },
                ),
            )
        gate = _named(record, "pure-python")

        self.assertIn("pure-python", record["failed_gates"])
        self.assertEqual(gate["data"]["build_requires_compilers"], ["cython"])
        self.assertIn("compiler is in the build back end", gate["detail"])
        self.assertIn("Cython>=0.29.25", gate["detail"])

    def test_a_wheel_only_hook_passes_with_a_source_tree_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "pyproject.toml": (
                            "[build-system]\n"
                            'requires = ["hatchling", "hatch-cython", "Cython"]\n'
                            "\n"
                            "[tool.hatch.build.targets.wheel.hooks.cython]\n"
                            'dependencies = ["hatch-cython"]\n'
                        )
                    }
                ),
            )
        gate = _named(record, "pure-python")

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(gate["data"]["environment_plan"], "source-tree")
        self.assertIn("hooks.cython", gate["data"]["wheel_only_hook"])
        self.assertIn("source-tree", gate["detail"])

    def test_hatch_cython_in_requires_alone_is_not_read_as_a_wheel_only_hook(
        self,
    ) -> None:
        # The requires line names the hook package. Only a configured hook table
        # means the source tree stays importable.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "pyproject.toml": (
                            "[build-system]\n"
                            'requires = ["hatchling", "hatch-cython"]\n'
                        )
                    }
                ),
            )
        gate = _named(record, "pure-python")

        self.assertIsNone(gate["data"]["wheel_only_hook"])
        self.assertIn("pure-python", record["failed_gates"])

    def test_a_plain_pyproject_leaves_the_language_gate_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "pyproject.toml": (
                            "[build-system]\n"
                            'requires = ["hatchling"]\n'
                            "\n[project]\n"
                            'dependencies = ["cython-free-lib"]\n'
                        )
                    }
                ),
            )
        gate = _named(record, "pure-python")

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(gate["data"]["build_requires_compilers"], [])
        self.assertIn("no compiler markers", gate["detail"])

    def test_a_policy_that_refuses_ai_work_fails_the_policy_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "CONTRIBUTING.md": (
                            "## Rules\n\nAI-generated pull requests will be "
                            "closed without review.\n"
                        )
                    }
                ),
            )

        self.assertIn("policy", record["failed_gates"])
        self.assertIn("refuses AI-assisted work", _named(record, "policy")["detail"])

    def test_a_policy_requiring_disclosure_passes_and_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "CONTRIBUTING.md": (
                            "Any AI-assisted contribution must be disclosed in "
                            "the pull request body.\n"
                        )
                    }
                ),
            )
        gate = _named(record, "policy")

        self.assertNotIn("policy", record["failed_gates"])
        self.assertTrue(gate["data"]["requires_disclosure"])

    def test_a_guide_that_permits_code_but_requires_own_words_records_it(
        self,
    ) -> None:
        # freqtrade's "AI Assisted Contributions" section permits the code and
        # forbids the prose, and the gate used to call that a clean pass.
        # https://github.com/wolfgang-aura/Mailman/issues/43
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "CONTRIBUTING.md": (
                            "## AI Assisted Contributions\n\n"
                            "- **Never let an LLM speak for you** - all comments, "
                            "issues and PR descriptions should be written in your "
                            "own words.\n"
                            "- Commits must be linked to your own account, not "
                            "some generic AI account.\n"
                        )
                    }
                ),
            )
        gate = _named(record, "policy")

        self.assertEqual(record["verdict"], "pass")
        self.assertTrue(gate["passed"])
        self.assertTrue(gate["data"]["requires_own_words"])
        self.assertTrue(gate["data"]["requires_human_account"])
        self.assertEqual(
            [entry["kind"] for entry in gate["data"]["constraints"]],
            ["own-words", "human-account"],
        )
        self.assertIn("constrains the submission", gate["detail"])
        self.assertIn("requires_own_words", gate["detail"])

    def test_a_machine_learning_guide_is_not_read_as_an_ai_ban(self) -> None:
        # "AI" appears in every model library's contributing guide. Matching it
        # loosely would reject the whole category.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "CONTRIBUTING.md": (
                            "This project builds AI agents. Please run the AI "
                            "evaluation suite before opening a pull request.\n"
                        )
                    }
                ),
            )

        self.assertNotIn("policy", record["failed_gates"])
        self.assertEqual(record["verdict"], "pass")

    def test_a_fully_claimed_tracker_fails_the_saturation_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    issues=[_issue(10), _issue(11)],
                    open_pulls=[
                        {
                            "number": 90,
                            "state": "open",
                            "title": "fix #10",
                            "body": "closes #11",
                            "head": {"ref": "issue-10"},
                            "user": {"login": "alice", "type": "User"},
                        }
                    ],
                ),
            )

        self.assertIn("saturation", record["failed_gates"])
        self.assertEqual(_named(record, "saturation")["data"]["unclaimed"], 0)

    def test_an_assigned_issue_does_not_count_as_available_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(issues=[_issue(10, assignee={"login": "someone"})]),
            )
        gate = _named(record, "saturation")

        self.assertEqual(gate["data"]["open_issues"], 1)
        self.assertEqual(gate["data"]["unassigned"], 0)

    def test_stars_never_decide_the_verdict(self) -> None:
        # Provenance reads stars too, so the contributor route has to carry this
        # repository instead. Otherwise the fixture would be testing provenance.
        crowd = [
            _pull(index, author=f"author{index}", merged_days_ago=index + 1)
            for index in range(12)
        ]
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    closed_pulls=crowd,
                    meta={
                        "full_name": "example/project",
                        "stargazers_count": 3,
                        "default_branch": "main",
                        "archived": False,
                        "created_at": _days_ago(30),
                        "fork": False,
                    },
                ),
            )
        stars = _named(record, "stars")

        self.assertEqual(record["verdict"], "pass")
        self.assertFalse(stars["blocking"])
        self.assertEqual(stars["data"]["stars"], 3)

    def test_a_young_thin_repository_is_refused_before_its_code_runs(self) -> None:
        # Nothing else here fails: the merges are fresh, the suite runs, the
        # Python is clean. The objection is that nobody but the author has read
        # the build back end that prepare-environment is about to execute.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    meta={
                        "full_name": "example/project",
                        "stargazers_count": 11,
                        "default_branch": "main",
                        "archived": False,
                        "created_at": _days_ago(20),
                        "fork": False,
                    }
                ),
            )
        gate = _named(record, "provenance")

        self.assertEqual(record["verdict"], "fail")
        self.assertEqual(record["failed_gates"], ["provenance"])
        self.assertEqual(gate["name"], "provenance")
        self.assertEqual(gate["data"]["age_days"], 20)
        self.assertEqual(gate["data"]["stars"], 11)

    def test_a_long_standing_repository_passes_on_age_and_stars(self) -> None:
        # pmorissette/ffn is this shape: 2638 stars since 2014, 11 outside
        # authors in ninety days, which is under the contributor threshold.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    meta={
                        "full_name": "example/project",
                        "stargazers_count": 2638,
                        "default_branch": "main",
                        "archived": False,
                        "created_at": _days_ago(4400),
                        "fork": False,
                    }
                ),
            )
        gate = _named(record, "provenance")

        self.assertEqual(record["verdict"], "pass")
        self.assertTrue(gate["passed"])
        self.assertIn("2638 star(s)", gate["detail"])

    def test_a_fork_is_refused_however_popular_the_upstream_is(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    meta={
                        "full_name": "example/project",
                        "stargazers_count": 90000,
                        "default_branch": "main",
                        "archived": False,
                        "created_at": _days_ago(4400),
                        "fork": True,
                    }
                ),
            )
        gate = _named(record, "provenance")

        self.assertEqual(record["failed_gates"], ["provenance"])
        self.assertIn("fork", gate["detail"])

    def test_an_archived_repository_stops_before_any_other_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            gh = FakeGitHub(
                meta={
                    "full_name": "example/project",
                    "stargazers_count": 100,
                    "default_branch": "main",
                    "archived": True,
                }
            )
            record = _screen(Path(temporary), gh)

        self.assertEqual(record["verdict"], "fail")
        self.assertEqual(record["failed_gates"], ["archived"])
        self.assertEqual(len(record["gates"]), 1)
        self.assertEqual(len(gh.asked), 1)

    def test_an_unreadable_repository_is_not_a_pass(self) -> None:
        def failing(arguments, **keywords):
            return _Result("", exit_code=1)

        with tempfile.TemporaryDirectory() as temporary:
            record = screen_repository(
                "example/project",
                data_root=Path(temporary),
                executable="gh",
                working_directory=Path(temporary),
                _execute=failing,
            )

        self.assertFalse(record["success"])
        self.assertNotIn("verdict", record)
        self.assertIn("could not be read", record["detail"])

    def test_the_verdict_is_cached_so_a_candidate_is_screened_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _screen(root, FakeGitHub())
            cached = load_screen(root, "example/project")

            self.assertIsNotNone(cached)
            self.assertEqual(cached["repository"], "example/project")
            self.assertTrue(
                (root / "screens" / "example__project.json").is_file()
            )

    def test_the_rendered_screen_names_every_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), FakeGitHub())
        rendered = render_screen(record)

        for name in ("freshness", "ci", "pure-python", "policy", "saturation", "stars"):
            with self.subTest(gate=name):
                self.assertIn(name, rendered)


if __name__ == "__main__":
    unittest.main()
