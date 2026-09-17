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
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from mailman.cli import main
from mailman.screen import (
    direct_push_share,
    forbids_duplicate_pull_requests,
    load_screen,
    render_screen,
    requires_prior_discussion,
    screen_repository,
    screen_shortlist,
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


def _issue(
    number: int, *, days_old: int = 3, assignee: object = None, labels: object = None
) -> dict:
    return {
        "number": number,
        "title": f"bug {number}",
        "created_at": _days_ago(days_old),
        "assignee": assignee,
        "labels": labels or [],
        "comments": 0,
    }


def _outside_pull(
    number: int,
    *,
    opened_days_ago: int,
    author: str = "carol",
    association: str = "CONTRIBUTOR",
    account_type: str = "User",
    merged: bool = False,
    closed: bool = False,
) -> dict:
    """A pull request as `pulls?state=all` lists it, decided three days after opening."""
    decided = merged or closed
    decided_at = _days_ago(max(opened_days_ago - 3, 0)) if decided else None
    return {
        "number": number,
        "title": f"fix {number}",
        "user": {"login": author, "type": account_type},
        "author_association": association,
        "created_at": _days_ago(opened_days_ago),
        "merged_at": decided_at if merged else None,
        "closed_at": decided_at,
        "state": "closed" if decided else "open",
        "head": {"ref": f"fix-{number}"},
        "body": "",
    }


def _response(
    days_ago: int,
    *,
    login: str = "maint",
    association: str = "MEMBER",
    account_type: str = "User",
    field: str = "submitted_at",
) -> dict:
    """One review or comment row, stamped in whichever field its endpoint uses."""
    return {
        "user": {"login": login, "type": account_type},
        "author_association": association,
        field: _days_ago(days_ago),
        "body": "looking",
    }


#: Three outside pull requests in the window, each reviewed a day after it
#: opened. The default fixture is responsive so that every other gate's test
#: still passes on it.
RESPONSIVE_PULLS = [
    _outside_pull(101, opened_days_ago=30, merged=True),
    _outside_pull(102, opened_days_ago=20, merged=True),
    _outside_pull(103, opened_days_ago=10),
]
RESPONSIVE_REVIEWS = {101: [_response(29)], 102: [_response(19)], 103: [_response(9)]}


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
        #: What `pulls?state=all` lists, which the responsiveness gate reads.
        self.all_pulls = overrides.pop("all_pulls", RESPONSIVE_PULLS)
        #: Reviews and inline review comments per pull request number.
        self.reviews = overrides.pop("reviews", RESPONSIVE_REVIEWS)
        self.review_comments = overrides.pop("review_comments", {})
        self.commits = overrides.pop(
            "commits", [{"sha": f"c{index}"} for index in range(12)]
        )
        #: The shas the repository pushed straight to the default branch, which
        #: the API answers for by naming no pull request.
        self.direct_pushes = set(overrides.pop("direct_pushes", ()))
        self.issues = overrides.pop("issues", [_issue(10), _issue(11)])
        self.issue_comments = overrides.pop("issue_comments", {})
        self.languages = overrides.pop("languages", {"Python": 100000})
        self.workflows = overrides.pop(
            "workflows", {"ci.yml": HEALTHY_WORKFLOW}
        )
        self.root = overrides.pop("root", [{"name": "pyproject.toml"}])
        self.policies = overrides.pop("policies", {})
        self.assignment_matches = overrides.pop("assignment_matches", 0)
        self.assignment_pulls = overrides.pop("assignment_pulls", [])
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
        if base == "search/issues":
            return {
                "total_count": self.assignment_matches,
                "items": self.assignment_pulls,
            }
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
        if "/commits/" in base and base.endswith("/pulls"):
            sha = base.rsplit("/", 2)[-2]
            return [] if sha in self.direct_pushes else [{"number": 1}]
        if base.endswith("/commits"):
            return self.commits if "page=1" in path or "page=" not in path else []
        if base.endswith("/reviews"):
            number = int(base.rsplit("/", 2)[-2])
            return self.reviews.get(number, [])
        if "/pulls/" in base and base.endswith("/comments"):
            number = int(base.rsplit("/", 2)[-2])
            return self.review_comments.get(number, [])
        if "/pulls" in base:
            if "state=all" in path:
                rows = self.all_pulls
            else:
                rows = self.open_pulls if "state=open" in path else self.closed_pulls
            return rows if "page=1" in path or "page=" not in path else []
        if "/comments" in base:
            # repos/<slug>/issues/<number>/comments, one thread per call.
            number = int(base.rsplit("/", 2)[-2])
            return self.issue_comments.get(number, [])
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

    def test_repeated_assignment_bot_closures_reject_the_repository(self) -> None:
        marker = {
            "user": {"login": "policy[bot]", "type": "Bot"},
            "body": "<!-- require-issue-link --> Closed because you are not assigned.",
        }
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    assignment_matches=637,
                    assignment_pulls=[{"number": 91}],
                    issue_comments={91: [marker]},
                ),
            )

        self.assertEqual(record["verdict"], "fail")
        self.assertIn("assignment", record["failed_gates"])
        gate = _named(record, "assignment")
        self.assertEqual(gate["data"]["marker"], "require-issue-link")
        self.assertEqual(gate["data"]["verified_occurrences"], 1)
        self.assertIn("#91", gate["detail"])

    def test_a_workflow_that_closes_unassigned_pull_requests_rejects(self) -> None:
        # pydantic/pydantic-ai#8164 and #7146 were closed by pr-guard.yml,
        # which the screen passed on 2026-09-14 because no marker matched.
        guard = (
            "name: PR Guard\n"
            "on: pull_request_target\n"
            "jobs:\n"
            "  guard:\n"
            "    steps:\n"
            "      - run: |\n"
            "          # Contributors should discuss and be assigned an issue before\n"
            "          # opening a PR. Issue and bot authors are exempt from this requirement.\n"
            "          gh pr comment $PR --body 'please wait to be assigned before opening a PR.'\n"
            "          gh pr close $PR\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(workflows={"ci.yml": HEALTHY_WORKFLOW, "pr-guard.yml": guard}),
            )

        self.assertEqual(record["verdict"], "fail")
        self.assertIn("assignment", record["failed_gates"])
        gate = _named(record, "assignment")
        self.assertEqual(gate["data"]["workflow_rule"]["workflow"], "pr-guard.yml")
        self.assertTrue(gate["data"]["workflow_rule"]["issue_author_exempt"])
        self.assertIn("pr-guard.yml", gate["detail"])
        self.assertIn("issue authors", gate["detail"])

    def test_loose_search_hits_without_the_exact_marker_do_not_reject(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    assignment_matches=3,
                    assignment_pulls=[{"number": 91}],
                    issue_comments={
                        91: [{"user": {"type": "Bot"}, "body": "issue link"}]
                    },
                ),
            )

        self.assertNotIn("assignment", record["failed_gates"])

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

    def test_notebooks_do_not_count_against_the_language_gate(self) -> None:
        # domokane/FinancePy: notebooks with saved outputs outweigh the library
        # by bytes, and nothing generates Python from a notebook.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    languages={"Jupyter Notebook": 780000, "Python": 220000}
                ),
            )
        gate = _named(record, "pure-python")

        self.assertNotIn("pure-python", record["failed_gates"])
        self.assertEqual(gate["data"]["python_share"], 1.0)

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

    def test_sentrys_refusal_phrased_as_an_outcome_fails_the_gate(self) -> None:
        # getsentry/sentry-python's "AI Use" section, verbatim. The gate passed
        # it with `constraints: []` and a hunt spent its only run on a patch
        # this paragraph describes and refuses.
        # https://github.com/wolfgang-aura/Mailman/issues/99
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "CONTRIBUTING.md": (
                            "### AI Use\n\nYou are welcome to use whatever tools "
                            "you prefer for making a contribution. However, any "
                            "changes you propose have to be reviewed and tested "
                            "by you, a human, first, before you submit a pull "
                            "request with them for the Sentry team to review. If "
                            "we feel like that didn't happen, we will close the "
                            "PR outright. For example, we won't review visibly "
                            "AI-generated PRs from an agent instructed to look "
                            'for and "fix" open issues in the repo.\n'
                        )
                    }
                ),
            )
        gate = _named(record, "policy")

        self.assertIn("policy", record["failed_gates"])
        self.assertIn(
            "we won't review visibly AI-generated PRs from an agent instructed "
            "to look for and",
            gate["data"]["quote"],
        )

    def test_a_required_maintainer_conversation_is_recorded_as_a_constraint(
        self,
    ) -> None:
        # The "Automated Checks" section of the same guide. This one is not a
        # rule about how the patch is written: it decides whether we may file.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "CONTRIBUTING.md": (
                            "### Automated Checks\n\nTo maintain the quality of "
                            "contributions, we use automated workflows that "
                            "enforce the following rules for PRs from "
                            "non-maintainers:\n\n"
                            "- **Issue reference required.** Your PR body must "
                            "reference a GitHub issue in the `getsentry` "
                            "organization.\n"
                            "- **Prior discussion required.** The referenced "
                            "issue must show a conversation between you and a "
                            "maintainer. Opening the issue counts as "
                            "participation - but a maintainer must have also "
                            "responded.\n\n"
                            "PRs that don't meet these criteria are "
                            "automatically closed and labeled "
                            "`violating-contribution-guidelines`.\n"
                        )
                    }
                ),
            )
        gate = _named(record, "policy")

        self.assertEqual(record["verdict"], "pass")
        self.assertTrue(gate["passed"])
        self.assertTrue(gate["data"]["requires_prior_discussion"])
        self.assertIn(
            "prior-discussion",
            [entry["kind"] for entry in gate["data"]["constraints"]],
        )
        quote = next(
            entry["quote"]
            for entry in gate["data"]["constraints"]
            if entry["kind"] == "prior-discussion"
        )
        self.assertIn("Prior discussion required", quote)
        self.assertIn("conversation between you and a maintainer", quote)
        self.assertEqual(
            requires_prior_discussion(record)["quote"],
            quote,
        )

    #: python-attrs/cattrs, verbatim: the guide says one sentence about LLM
    #: tools and keeps the rule in another repository.
    CATTRS_GUIDE = (
        "# Contributing\n\n"
        "Thank you for considering contributing to *cattrs*!\n\n"
        "> [!IMPORTANT]\n"
        "> If you use LLM / \"AI\" tools for your contributions, please read "
        "and follow our [_Generative AI / LLM Policy_][llm].\n\n"
        "Every contribution helps, and credit will always be given.\n\n"
        "[llm]: https://github.com/python-attrs/.github/blob/main/AI_POLICY.md\n"
    )
    #: python-attrs/.github/AI_POLICY.md, the document the guide links.
    ATTRS_AI_POLICY = (
        "# Generative AI / LLM Policy\n\n"
        "We are not opposed to tools, but:\n\n"
        "- Absolutely **no** unsupervised agentic tools.\n"
        "- Pull requests that have an LLM product listed as co-author can't "
        "be merged.\n"
    )

    def test_a_guide_that_links_its_ai_policy_is_gated_on_that_policy(self) -> None:
        # The gate stopped at CONTRIBUTING.md, never followed the link, and
        # recorded `constraints: []` for a project whose policy refuses
        # unsupervised agentic tools by name.
        gh = FakeGitHub(
            policies={
                "CONTRIBUTING.md": self.CATTRS_GUIDE,
                "AI_POLICY.md": self.ATTRS_AI_POLICY,
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), gh)
        gate = _named(record, "policy")

        self.assertEqual(record["verdict"], "fail")
        self.assertIn("policy", record["failed_gates"])
        self.assertEqual(gate["data"]["result"], "refused")
        # The document that decided is named, not the file the gate started in.
        self.assertEqual(
            gate["data"]["source"], "python-attrs/.github/AI_POLICY.md"
        )
        self.assertEqual(gate["data"]["guide"], "CONTRIBUTING.md")
        self.assertIn("agentic tools", gate["data"]["quote"])
        self.assertEqual(
            [entry["source"] for entry in gate["data"]["followed_documents"]],
            ["python-attrs/.github/AI_POLICY.md"],
        )
        # Followed through the same fetch path, into the organization's
        # `.github` repository rather than the target's own.
        self.assertIn(
            "repos/python-attrs/.github/contents/AI_POLICY.md?ref=main", gh.asked
        )

    def test_a_linked_policy_that_cannot_be_read_is_unknown_not_permitted(
        self,
    ) -> None:
        gh = FakeGitHub(policies={"CONTRIBUTING.md": self.CATTRS_GUIDE})
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), gh)
        gate = _named(record, "policy")

        self.assertEqual(record["verdict"], "fail")
        self.assertIn("policy", record["failed_gates"])
        self.assertEqual(gate["data"]["result"], "unknown")
        self.assertEqual(
            [entry["source"] for entry in gate["data"]["unread_documents"]],
            ["python-attrs/.github/AI_POLICY.md"],
        )
        self.assertIn("unknown", gate["detail"])

    def test_a_relative_link_is_read_beside_the_guide_that_names_it(self) -> None:
        gh = FakeGitHub(
            policies={
                ".github/CONTRIBUTING.md": (
                    "Please read our [AI policy](AI_POLICY.md) before "
                    "opening a pull request.\n"
                ),
                ".github/AI_POLICY.md": (
                    "AI-generated pull requests will be closed without review.\n"
                ),
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), gh)
        gate = _named(record, "policy")

        self.assertIn("policy", record["failed_gates"])
        self.assertEqual(gate["data"]["source"], ".github/AI_POLICY.md")
        self.assertIn("repos/example/project/contents/.github/AI_POLICY.md", gh.asked)

    def test_a_link_that_is_not_about_ai_costs_no_call(self) -> None:
        # Every guide links a code of conduct. Following all of them would turn
        # one gate into an API budget.
        gh = FakeGitHub(
            policies={
                "CONTRIBUTING.md": (
                    "Read the [Code of Conduct](CODE_OF_CONDUCT.md) and the "
                    "[maintainers' handbook](https://example.invalid/handbook) "
                    "first.\n"
                )
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), gh)
        gate = _named(record, "policy")

        self.assertEqual(record["verdict"], "pass")
        self.assertEqual(gate["data"]["result"], "permitted")
        self.assertEqual(gate["data"]["followed_documents"], [])
        self.assertNotIn(
            "repos/example/project/contents/CODE_OF_CONDUCT.md", gh.asked
        )

    def test_a_constraint_in_a_linked_policy_reaches_the_record(self) -> None:
        gh = FakeGitHub(
            policies={
                "CONTRIBUTING.md": "See our [LLM policy](AI_POLICY.md).\n",
                "AI_POLICY.md": (
                    "Any AI-assisted contribution must be disclosed in the "
                    "pull request body.\n"
                ),
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), gh)
        gate = _named(record, "policy")

        self.assertEqual(record["verdict"], "pass")
        self.assertTrue(gate["data"]["requires_disclosure"])
        self.assertEqual(
            [entry["source"] for entry in gate["data"]["constraints"]],
            ["AI_POLICY.md"],
        )
        self.assertIn("AI_POLICY.md", gate["detail"])

    def test_a_rule_against_duplicate_pull_requests_is_recorded(self) -> None:
        # urllib3's contributing guide and README, verbatim. Nothing read this,
        # so the stale-attempt rule offered to supersede a dormant attempt in a
        # repository that closes the second pull request unread.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "CONTRIBUTING.md": (
                            "## Pull requests\n\nDuplicate pull requests for "
                            "the same issue, including alternative solutions, "
                            "will be rejected without review unless a "
                            "maintainer has approved opening an alternative "
                            "pull request in advance.\n"
                        )
                    }
                ),
            )
        gate = _named(record, "policy")

        # The rule is about which pull requests they read, not about AI, so the
        # gate passes and the constraint travels.
        self.assertEqual(record["verdict"], "pass")
        self.assertTrue(gate["data"]["forbids_duplicate_pull_requests"])
        constraint = forbids_duplicate_pull_requests(record)
        self.assertEqual(constraint["kind"], "no-duplicate-pull-requests")
        self.assertIn("Duplicate pull requests", constraint["quote"])
        self.assertIn("rejected without review", constraint["quote"])
        self.assertIn("without review", gate["detail"])

    def test_a_guide_with_no_duplicate_rule_records_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "CONTRIBUTING.md": (
                            "Open a pull request against `main` and keep it "
                            "focused on one change.\n"
                        )
                    }
                ),
            )
        gate = _named(record, "policy")

        self.assertFalse(gate["data"]["forbids_duplicate_pull_requests"])
        self.assertIsNone(forbids_duplicate_pull_requests(record))

    def test_a_welcome_with_a_stale_pull_request_rule_is_not_a_refusal(self) -> None:
        # Two rules about two different things, a sentence apart. Reading the
        # closure rule as an answer to the AI rule would reject a repository
        # that says the work is welcome.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    policies={
                        "CONTRIBUTING.md": (
                            "We use AI in review ourselves. AI-assisted "
                            "contributions are welcome, and any AI-assisted "
                            "contribution must be disclosed in the pull request "
                            "body. Stale pull requests will be closed after 30 "
                            "days of inactivity.\n"
                        )
                    }
                ),
            )
        gate = _named(record, "policy")

        self.assertEqual(record["verdict"], "pass")
        self.assertNotIn("policy", record["failed_gates"])
        self.assertTrue(gate["data"]["requires_disclosure"])
        self.assertFalse(gate["data"]["requires_prior_discussion"])

    def test_a_closure_rule_about_something_else_is_not_an_ai_refusal(self) -> None:
        # Both sentences are real, from guides screened in this hunt. anyio
        # closes a pull request that erases the template; securo declines to
        # review the tool, which is a statement about who is accountable.
        for guide in (
            "Contributions written with AI assistance are welcome. Do not "
            "erase or replace the template contents - PRs that do so will be "
            "closed without review.",
            "**We don't review the AI, we review you.** When a PR arrives, the "
            "questions are the same as they have always been: does this person "
            "understand what they are proposing?",
        ):
            with self.subTest(guide=guide[:40]):
                with tempfile.TemporaryDirectory() as temporary:
                    record = _screen(
                        Path(temporary),
                        FakeGitHub(policies={"CONTRIBUTING.md": guide}),
                    )
                self.assertEqual(record["verdict"], "pass")
                self.assertNotIn("policy", record["failed_gates"])

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

    def test_an_unanswered_work_claim_in_comments_counts_as_a_claim(self) -> None:
        # openai/openai-agents-python on 2026-09-06: the claim that decides the
        # tracker is a comment, not GitHub linkage. See
        # https://github.com/wolfgang-aura/Mailman/issues/53.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    issues=[_issue(10), _issue(11)],
                    issue_comments={
                        11: [
                            {
                                "user": {"login": "rival", "type": "User"},
                                "author_association": "NONE",
                                "body": "I'm working on this, a fix is ready.",
                                "created_at": _days_ago(1),
                            }
                        ]
                    },
                ),
            )
        gate = _named(record, "saturation")

        self.assertEqual(gate["data"]["claimed_by_comment"], 1)
        self.assertEqual(gate["data"]["unclaimed"], 1)
        self.assertIn("no claim of any kind", gate["detail"])

    def test_an_unclaimed_tracker_that_is_all_enhancements_and_stale_fails(
        self) -> None:
        # The four free issues on openai/openai-agents-python were an OIDC
        # request, a February tracing report, a defaults change, and a
        # localization enhancement: nine nominal openings, no work.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    issues=[
                        _issue(10, days_old=1, labels=["enhancement"]),
                        _issue(11, days_old=400),
                        _issue(12, days_old=2, labels=["feature-request"]),
                    ],
                    issue_comments={11: []},
                ),
            )
        gate = _named(record, "saturation")

        self.assertIn("saturation", record["failed_gates"])
        self.assertEqual(gate["data"]["unclaimed"], 3)
        self.assertEqual(gate["data"]["workable"], 0)
        self.assertIn("none is workable", gate["detail"])
        self.assertIn("enhancement-labelled", gate["detail"])
        self.assertIn("older than the 90-day issue window", gate["detail"])

    def test_a_three_week_old_backlog_is_workable_under_a_fresh_window(self) -> None:
        # The eighteen repositories that failed nothing but saturation, among
        # them fsspec/filesystem_spec with 282 unclaimed issues: outside work
        # merges every week, and every unclaimed bug is older than a fortnight.
        # https://github.com/wolfgang-aura/Mailman/issues/95
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    closed_pulls=[
                        _pull(1, author="alice", merged_days_ago=2),
                        _pull(2, author="bob", merged_days_ago=5),
                    ],
                    issues=[_issue(10, days_old=30), _issue(11, days_old=30)],
                    issue_comments={10: [], 11: []},
                ),
            )
        gate = _named(record, "saturation")

        self.assertEqual(record["verdict"], "pass")
        self.assertNotIn("saturation", record["failed_gates"])
        self.assertEqual(gate["data"]["workable"], 2)
        self.assertEqual(gate["data"]["stale_beyond_window"], 0)
        self.assertEqual(gate["data"]["median_workable_age_days"], 30)
        self.assertEqual(gate["data"]["window_days"], 14)
        self.assertEqual(gate["data"]["issue_window_days"], 90)
        self.assertEqual(record["issue_window_days"], 90)

    def test_the_issue_window_is_set_apart_from_the_merge_window(self) -> None:
        # Passing the merge window as the age cap is the defect itself, so the
        # two have to be settable apart.
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    issues=[_issue(10, days_old=30)],
                    issue_comments={10: []},
                ),
                issue_window_days=14,
            )
        gate = _named(record, "saturation")

        self.assertIn("saturation", record["failed_gates"])
        self.assertEqual(gate["data"]["stale_beyond_window"], 1)
        self.assertIn("older than the 14-day issue window", gate["detail"])
        self.assertIn("counted over 14 days", gate["detail"])

    def test_the_workable_count_excludes_labels_and_staleness_from_the_median(
        self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    issues=[
                        _issue(10, days_old=3),
                        _issue(11, days_old=400),
                        _issue(12, days_old=1, labels=["enhancement"]),
                    ],
                    issue_comments={10: []},
                ),
            )
        gate = _named(record, "saturation")

        self.assertNotIn("saturation", record["failed_gates"])
        self.assertEqual(gate["data"]["unclaimed"], 3)
        self.assertEqual(gate["data"]["workable"], 1)
        self.assertEqual(gate["data"]["median_workable_age_days"], 3)
        self.assertIn(
            f"{gate['data']['unclaimed']} of "
            f"{gate['data']['unassigned']} unassigned issue(s) have no claim "
            "of any kind, 1 of them workable",
            gate["detail"],
        )

    def test_a_reviewed_repository_records_a_low_direct_push_share(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), FakeGitHub())
        gate = _named(record, "direct-push")

        self.assertTrue(gate["passed"])
        self.assertFalse(gate["blocking"])
        self.assertEqual(gate["data"]["commits_sampled"], 12)
        self.assertEqual(gate["data"]["direct_push_share"], 0.0)
        self.assertEqual(direct_push_share(record), 0.0)

    def test_commits_that_skip_a_pull_request_are_counted_and_warned_about(
        self) -> None:
        # pdm-project/pdm#3884: the maintainer fixed the issue directly on main
        # in dc4e314 while our correct pull request sat unreviewed.
        # https://github.com/wolfgang-aura/Mailman/issues/79
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(direct_pushes={f"c{index}" for index in range(8)}),
            )
        gate = _named(record, "direct-push")

        self.assertFalse(gate["passed"])
        self.assertFalse(gate["blocking"])
        self.assertEqual(record["verdict"], "pass")
        self.assertNotIn("direct-push", record["failed_gates"])
        self.assertEqual(gate["data"]["direct_pushes"], 8)
        self.assertEqual(gate["data"]["through_pull_request"], 4)
        self.assertEqual(gate["data"]["direct_push_share"], 0.67)
        self.assertEqual(gate["data"]["default_branch"], "main")
        self.assertIn("outside a pull request", gate["detail"])
        self.assertEqual(direct_push_share(record), 0.67)

    def test_a_short_history_does_not_carry_a_direct_push_habit(self) -> None:
        # A share computed over four commits is arithmetic, not a habit.
        commits = [{"sha": f"c{index}"} for index in range(4)]
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    commits=commits,
                    direct_pushes={commit["sha"] for commit in commits},
                ),
            )
        gate = _named(record, "direct-push")

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["data"]["commits_sampled"], 4)
        self.assertEqual(gate["data"]["direct_push_share"], 1.0)

    def test_a_screen_without_the_gate_reports_an_unknown_share(self) -> None:
        self.assertIsNone(direct_push_share(None))
        self.assertIsNone(direct_push_share({"gates": []}))

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

        for name in (
            "freshness", "ci", "pure-python", "policy", "saturation",
            "direct-push", "responsiveness", "stars",
        ):
            with self.subTest(gate=name):
                self.assertIn(name, rendered)


class ResponsivenessTests(unittest.TestCase):
    """How long a stranger's pull request waits for a maintainer's first word.

    Twelve pull requests filed since 2026-09-01: one merged, six closed
    unmerged, and the closes came from repositories where an outside pull
    request waits weeks for a first response. Every one of them passed
    freshness.
    """

    def test_a_responsive_repository_passes_with_its_numbers_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), FakeGitHub())
        gate = _named(record, "responsiveness")

        self.assertTrue(gate["passed"])
        self.assertTrue(gate["blocking"])
        self.assertNotIn("responsiveness", record["failed_gates"])
        self.assertEqual(record["schema_version"], 3)
        self.assertEqual(record["responsiveness_days"], 90)
        self.assertEqual(gate["data"]["result"], "pass")
        self.assertEqual(gate["data"]["sampled"], 3)
        self.assertEqual(gate["data"]["responded"], 3)
        self.assertEqual(gate["data"]["responded_within_days"], 3)
        self.assertEqual(gate["data"]["response_share"], 1.0)
        self.assertEqual(gate["data"]["median_first_response_days"], 1.0)
        self.assertEqual(gate["data"]["merged"], 2)
        self.assertEqual(gate["data"]["closed_unmerged"], 0)
        self.assertEqual(gate["data"]["still_open"], 1)
        self.assertEqual(gate["data"]["window_days"], 90)
        rendered = render_screen(record)
        self.assertIn("median first maintainer response 1.0 day(s)", rendered)
        self.assertIn("3 of 3 answered within 14 days (100%)", rendered)
        self.assertIn("2 merged, 0 closed unmerged", rendered)

    def test_a_slow_median_first_response_fails(self) -> None:
        # poetry and pdm are this shape: the pull request is read, eventually.
        pulls = [
            _outside_pull(201, opened_days_ago=80, merged=True),
            _outside_pull(202, opened_days_ago=70),
            _outside_pull(203, opened_days_ago=60),
            _outside_pull(204, opened_days_ago=50),
        ]
        reviews = {
            201: [_response(50)],
            202: [_response(40, field="created_at")],
            203: [_response(30)],
            204: [_response(20)],
        }
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary),
                FakeGitHub(
                    all_pulls=pulls,
                    reviews={201: reviews[201], 203: reviews[203], 204: reviews[204]},
                    issue_comments={202: reviews[202]},
                ),
            )
        gate = _named(record, "responsiveness")

        self.assertFalse(gate["passed"])
        self.assertIn("responsiveness", record["failed_gates"])
        self.assertEqual(gate["data"]["result"], "fail")
        self.assertEqual(gate["data"]["median_first_response_days"], 30.0)
        self.assertEqual(gate["data"]["responded"], 4)
        self.assertEqual(gate["data"]["responded_within_days"], 0)
        self.assertEqual(gate["data"]["response_share"], 0.0)
        self.assertIn("the median wait is over 14 days", gate["detail"])
        self.assertIn("under 50% were answered within 14 days", gate["detail"])

    def test_a_repository_that_closes_more_than_it_merges_fails(self) -> None:
        # Fast answers, and the answer is usually no.
        pulls = [
            _outside_pull(301, opened_days_ago=40, merged=True),
            _outside_pull(302, opened_days_ago=35, merged=True),
            _outside_pull(303, opened_days_ago=30, closed=True),
            _outside_pull(304, opened_days_ago=25, closed=True),
            _outside_pull(305, opened_days_ago=20, closed=True),
            _outside_pull(306, opened_days_ago=15, closed=True),
        ]
        reviews = {
            number: [_response(days - 1)]
            for number, days in zip(range(301, 307), (40, 35, 30, 25, 20, 15))
        }
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary), FakeGitHub(all_pulls=pulls, reviews=reviews)
            )
        gate = _named(record, "responsiveness")

        self.assertFalse(gate["passed"])
        self.assertEqual(gate["data"]["merged"], 2)
        self.assertEqual(gate["data"]["closed_unmerged"], 4)
        self.assertEqual(gate["data"]["response_share"], 1.0)
        self.assertEqual(
            gate["detail"].split(": ", 1)[1],
            "more outside pull requests were closed unmerged than merged",
        )

    def test_a_close_heavy_ratio_over_too_few_decisions_is_not_read(self) -> None:
        pulls = [
            _outside_pull(401, opened_days_ago=40, merged=True),
            _outside_pull(402, opened_days_ago=30, closed=True),
            _outside_pull(403, opened_days_ago=20, closed=True),
            _outside_pull(404, opened_days_ago=10),
        ]
        reviews = {401: [_response(39)], 402: [_response(29)], 403: [_response(19)], 404: [_response(9)]}
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary), FakeGitHub(all_pulls=pulls, reviews=reviews)
            )
        gate = _named(record, "responsiveness")

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["data"]["closed_unmerged"], 2)
        self.assertEqual(gate["data"]["merged"], 1)

    def test_too_few_outside_pull_requests_is_unknown_and_fails(self) -> None:
        pulls = [
            _outside_pull(501, opened_days_ago=10, merged=True),
            _outside_pull(502, opened_days_ago=5, merged=True),
            # Outside the window, so not a sample however fast it was read.
            _outside_pull(503, opened_days_ago=120, merged=True),
        ]
        reviews = {501: [_response(9)], 502: [_response(4)], 503: [_response(119)]}
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(
                Path(temporary), FakeGitHub(all_pulls=pulls, reviews=reviews)
            )
        gate = _named(record, "responsiveness")

        self.assertFalse(gate["passed"])
        self.assertIn("responsiveness", record["failed_gates"])
        self.assertEqual(gate["data"]["result"], "unknown")
        self.assertEqual(gate["data"]["sampled"], 2)
        self.assertEqual(gate["data"]["outside_pull_requests_in_window"], 2)
        self.assertEqual(gate["data"]["pull_requests_scanned"], 3)
        self.assertIn("unknown: 2 outside pull request(s) opened in 90 days", gate["detail"])
        self.assertIn("Unknown is not responsive", gate["detail"])

    def test_bots_and_maintainers_own_pull_requests_are_not_samples(self) -> None:
        pulls = [
            _outside_pull(
                601,
                opened_days_ago=40,
                author="dependabot[bot]",
                account_type="Bot",
                merged=True,
            ),
            _outside_pull(
                602, opened_days_ago=35, author="maint", association="MEMBER"
            ),
            _outside_pull(603, opened_days_ago=30, merged=True),
            _outside_pull(604, opened_days_ago=20, merged=True),
            _outside_pull(605, opened_days_ago=10),
        ]
        # 605 is answered only by a bot and by its own author, which is no
        # answer at all, so it has waited its whole age.
        reviews = {603: [_response(29)], 604: [_response(19)]}
        comments = {
            605: [
                _response(
                    9,
                    login="coderabbitai[bot]",
                    association="MEMBER",
                    account_type="Bot",
                    field="created_at",
                ),
                _response(
                    8, login="carol", association="CONTRIBUTOR", field="created_at"
                ),
            ]
        }
        with tempfile.TemporaryDirectory() as temporary:
            gh = FakeGitHub(all_pulls=pulls, reviews=reviews, issue_comments=comments)
            record = _screen(Path(temporary), gh)
        gate = _named(record, "responsiveness")

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["data"]["sampled"], 3)
        self.assertEqual(gate["data"]["responded"], 2)
        self.assertEqual(gate["data"]["responded_within_days"], 2)
        self.assertEqual(gate["data"]["response_share"], 0.67)
        self.assertEqual(gate["data"]["median_first_response_days"], 1.0)
        self.assertEqual(gate["data"]["excluded_bot_authors"], ["dependabot[bot]"])
        self.assertIn("excluded dependabot[bot]", gate["detail"])
        for number in (601, 602):
            self.assertNotIn(f"repos/example/project/pulls/{number}/reviews", gh.asked)

    def test_the_sample_is_capped_and_the_cap_recorded(self) -> None:
        pulls = [
            _outside_pull(700 + index, opened_days_ago=60 - index, merged=True)
            for index in range(55)
        ]
        reviews = {700 + index: [_response(59 - index)] for index in range(55)}
        with tempfile.TemporaryDirectory() as temporary:
            gh = FakeGitHub(all_pulls=pulls, reviews=reviews)
            record = _screen(Path(temporary), gh)
        gate = _named(record, "responsiveness")

        self.assertTrue(gate["passed"])
        self.assertEqual(gate["data"]["outside_pull_requests_in_window"], 55)
        self.assertEqual(gate["data"]["sampled"], 50)
        self.assertEqual(gate["data"]["sample_cap"], 50)
        self.assertEqual(
            sum(path.startswith("repos/example/project/pulls/7") and path.endswith("reviews?per_page=100&page=1") for path in gh.asked),
            50,
        )

    def test_the_window_is_settable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), FakeGitHub(), responsiveness_days=15)
        gate = _named(record, "responsiveness")

        self.assertEqual(record["responsiveness_days"], 15)
        self.assertEqual(gate["data"]["window_days"], 15)
        # Only the pull request opened ten days ago is inside a 15-day window.
        self.assertEqual(gate["data"]["sampled"], 1)
        self.assertEqual(gate["data"]["result"], "unknown")


def _reply(body: str, *, association: str = "NONE", days_ago: int = 1) -> dict:
    return {
        "user": {"login": "somebody", "type": "User"},
        "author_association": association,
        "body": body,
        "created_at": _days_ago(days_ago),
    }


class ShortlistTests(unittest.TestCase):
    """The saturation gate keeps the issues it found, ranked.

    Across the last two hunts 72 of 106 pre-screened issues died on somebody's
    open pull request, because the newest unclaimed issue is where everybody
    looks first. See https://github.com/wolfgang-aura/Mailman/issues/102.
    """

    def _shortlist(self, gh: FakeGitHub) -> tuple[dict, list[dict]]:
        with tempfile.TemporaryDirectory() as temporary:
            record = _screen(Path(temporary), gh)
        return record, screen_shortlist(record)

    def test_an_invited_recent_issue_outranks_an_old_uninvited_one(self) -> None:
        record, rows = self._shortlist(
            FakeGitHub(
                issues=[
                    _issue(10, days_old=60),
                    _issue(11, days_old=2),
                    _issue(12, days_old=60),
                ],
                issue_comments={
                    10: [],
                    11: [_reply("PRs welcome for this one.", association="MEMBER")],
                    12: [
                        _reply(
                            "Happy to accept a PR.", association="OWNER", days_ago=30
                        )
                    ],
                },
            )
        )
        by_number = {row["number"]: row for row in rows}
        gate = _named(record, "saturation")

        # Invited beats everything, then recent, then the tie falls to age.
        self.assertEqual([row["number"] for row in rows], [11, 12, 10])
        self.assertEqual(
            by_number[11]["reasons"],
            ["maintainer-invited", "recent", "no-linked-pr"],
        )
        self.assertEqual(
            by_number[12]["reasons"], ["maintainer-invited", "no-linked-pr"]
        )
        self.assertEqual(by_number[10]["reasons"], ["no-linked-pr"])
        self.assertGreater(by_number[12]["score"], by_number[10]["score"])
        self.assertEqual(gate["data"]["maintainer_invited"], 2)
        self.assertIn("2 asked for by a maintainer", gate["detail"])

    def test_an_old_invited_issue_outranks_a_recent_uninvited_one(self) -> None:
        # "Feel free to open a PR" with nobody having asked is an invitation to
        # anybody, not the work handed to somebody; the issue stays on the list.
        _, rows = self._shortlist(
            FakeGitHub(
                issues=[_issue(10, days_old=2), _issue(11, days_old=60)],
                issue_comments={
                    10: [],
                    11: [
                        _reply(
                            "Feel free to open a PR.",
                            association="COLLABORATOR",
                            days_ago=30,
                        )
                    ],
                },
            )
        )

        self.assertEqual([row["number"] for row in rows], [11, 10])

    def test_an_invitation_by_a_non_maintainer_does_not_count(self) -> None:
        _, rows = self._shortlist(
            FakeGitHub(
                issues=[_issue(10, days_old=60)],
                issue_comments={
                    10: [_reply("PRs welcome, I'd say.", association="CONTRIBUTOR")]
                },
            )
        )

        self.assertEqual(rows[0]["reasons"], ["no-linked-pr"])

    def test_a_maintainer_written_on_thread_is_recent(self) -> None:
        _, rows = self._shortlist(
            FakeGitHub(
                issues=[_issue(10, days_old=60)],
                issue_comments={
                    10: [_reply("Still happens on main.", association="MEMBER")]
                },
            )
        )

        self.assertEqual(rows[0]["reasons"], ["recent", "no-linked-pr"])

    def test_a_help_wanted_label_counts_as_an_invitation(self) -> None:
        _, rows = self._shortlist(
            FakeGitHub(
                issues=[
                    _issue(10, days_old=60, labels=[{"name": "Help-Wanted"}]),
                    _issue(11, days_old=60, labels=["good first issue"]),
                    _issue(12, days_old=60, labels=["bug"]),
                ],
                issue_comments={10: [], 11: [], 12: []},
            )
        )

        self.assertEqual([row["number"] for row in rows], [10, 11, 12])
        self.assertIn("maintainer-invited", rows[0]["reasons"])
        self.assertIn("maintainer-invited", rows[1]["reasons"])
        self.assertNotIn("maintainer-invited", rows[2]["reasons"])

    def test_a_pull_request_cited_in_the_thread_loses_the_clean_code(self) -> None:
        # Not a claim on its own: only `gh pr view` can say what #90 is. But an
        # issue nobody has cited anything against ranks above one somebody has.
        cited = _issue(10, days_old=3)
        cited["body"] = "See the earlier attempt in #90."
        _, rows = self._shortlist(
            FakeGitHub(
                issues=[cited, _issue(11, days_old=3)],
                issue_comments={10: [], 11: []},
            )
        )

        self.assertEqual([row["number"] for row in rows], [11, 10])
        self.assertEqual(rows[1]["reasons"], ["recent"])

    def test_the_rendered_screen_prints_the_shortlist_from_the_top(self) -> None:
        record, _ = self._shortlist(
            FakeGitHub(
                issues=[_issue(10, days_old=60), _issue(11, days_old=2)],
                issue_comments={
                    10: [],
                    11: [_reply("PRs welcome.", association="MEMBER")],
                },
            )
        )
        rendered = render_screen(record)

        self.assertIn("shortlist (ranked", rendered)
        self.assertLess(rendered.index("#11"), rendered.index("#10"))
        self.assertIn("maintainer-invited, recent, no-linked-pr", rendered)

    def test_screen_target_json_prints_the_ranked_shortlist(self) -> None:
        record, rows = self._shortlist(
            FakeGitHub(
                issues=[_issue(10, days_old=60), _issue(11, days_old=2)],
                issue_comments={
                    10: [],
                    11: [_reply("PRs welcome.", association="MEMBER")],
                },
            )
        )
        stdout = StringIO()
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "mailman.cli.screen_repository", return_value=record
        ), redirect_stdout(stdout):
            code = main(
                [
                    "screen-target",
                    "example/project",
                    "--refresh",
                    "--json",
                    "--data-root",
                    temporary,
                ]
            )
        printed = json.loads(stdout.getvalue())

        self.assertEqual(code, 0)
        self.assertEqual(printed["verdict"], "pass")
        self.assertEqual(printed["shortlist"], rows)
        self.assertEqual(printed["shortlist"][0]["number"], 11)


if __name__ == "__main__":
    unittest.main()
