"""A claim made in an issue's own comments is prior art the gate must see.

`openai/openai-agents-python` #4775 had no pull request against it, so the
duplicate search was empty and `check-target` called it unclaimed. Its second
comment was "I'd like to work on this issue", followed by a scope plan. See
https://github.com/wolfgang-aura/Mailman/issues/36.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.claims import (
    CLAIMS_FILENAME,
    classify_comment,
    classify_thread,
    design_open_questions,
    excludes_agents,
    is_maintainer_invitation,
    load_claims,
    maintainer_declines,
    pull_request_references,
    read_claims,
    render_claims,
    triage_warning,
)


def _comment(body: str, *, association: str = "NONE", login: str = "someone") -> dict:
    return {
        "body": body,
        "author_association": association,
        "created_at": "2026-09-01T00:00:00Z",
        "user": {"login": login, "type": "User"},
    }


class _Result:
    def __init__(self, stdout: str, exit_code: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = exit_code
        self.timed_out = False

    def to_dict(self) -> dict:
        return {"exit_code": self.exit_code, "timed_out": self.timed_out}


def _failing(arguments, **keywords):
    return _Result("", exit_code=1)


class _FakeGh:
    """Answer the three API paths `read_claims` asks for, and nothing else."""

    def __init__(
        self,
        issue: dict,
        comments: list[dict],
        timeline: list[dict] | None = None,
        elsewhere: dict[str, list[dict]] | None = None,
    ) -> None:
        self.issue = issue
        self.comments = comments
        self.timeline = timeline or []
        self.elsewhere = elsewhere or {}
        self.asked: list[str] = []

    def __call__(self, arguments, **keywords):
        # `[gh, "api", PATH, ...query fields]`: the path is the third word,
        # never the last one.
        path = arguments[2]
        self.asked.append(path)
        if path in self.elsewhere:
            payload: object = self.elsewhere[path]
        elif "/comments" in path:
            payload = self.comments
        elif "/timeline" in path:
            payload = self.timeline
        else:
            payload = self.issue
        return _Result(json.dumps(payload))


def _cross_reference(url: str) -> dict:
    return {"event": "cross-referenced", "source": {"issue": {"html_url": url}}}


class ClassifyCommentTests(unittest.TestCase):
    def test_pasted_tool_output_in_a_code_block_hands_nothing_over(self) -> None:
        # posit-dev/py-shiny#2497: pyright's "cannot be assigned to parameter"
        # in a fenced block read as "assigned to". Mailman #207.
        body = (
            "Returning a dict from a renderer fails to type-check.\n\n"
            "```\n"
            'error: Argument of type "() -> dict[str, int]" cannot be assigned '
            'to parameter "_fn"\n'
            "```\n\n"
            "pyright 1.1.x, shiny from `main`."
        )
        self.assertIsNone(classify_comment(_comment(body, association="COLLABORATOR")))

    def test_a_handover_outside_a_code_block_still_counts(self) -> None:
        body = "```\nprint('x')\n```\n\nGo ahead, it's all yours."
        self.assertEqual(
            classify_comment(_comment(body, association="MEMBER")), "assignment"
        )

    def test_the_openai_agents_comment_reads_as_a_claim(self) -> None:
        self.assertEqual(
            classify_comment(_comment("I'd like to work on this issue.")), "claim"
        )

    def test_the_comment_as_github_actually_stored_it_reads_as_a_claim(self) -> None:
        # The live comment on #4775 carries U+2019, not an ASCII apostrophe,
        # which is what the first version of this classifier missed.
        self.assertEqual(
            classify_comment(
                _comment(
                    "I’d like to work on this issue.\n\nPlanned scope:\n"
                    "- route resumed pending-input Session appends through the "
                    "existing fail-closed checkpoint;",
                    association="CONTRIBUTOR",
                )
            ),
            "claim",
        )

    def test_the_common_claim_phrasings_read_as_claims(self) -> None:
        for body in (
            "I'm working on this",
            "I am currently working on it",
            "I'll take this one",
            "I can take a look and open a PR",
            "please assign this to me",
            "assign me",
            "Can I work on this?",
            "picking this up now",
            "On it!",
            "let me take a stab at this",
            "I have a patch ready for this",
            "Taking this up, will send a PR shortly",
            "I would like to contribute a fix here",
            # domokane/FinancePy#266, #262, #267, #264: a reporter who has
            # the fix and offers it. See wolfgang-aura/Mailman#93.
            "I would be glad to prepare a focused PR and discuss a contract.",
            "whether the attached candidate is suitable for a PR?",
            "Proposed correction in `financepy/products/bonds/bond_zero.py`:",
            "I would be happy to help take this from a report to a fix",
            # quantumlib/Cirq#8317, a day-old comment the prescreen read as
            # no claim while the coordinator read it as one.
            "I am interested in working on this issue. I am quite new to open "
            "source contributions, so it may take me some time. If no one is "
            "assigned yet, could this issue be assigned to me?",
            "Could this be assigned to me?",
            # robotframework#5774's report, verbatim. Mailman #198.
            "I'm happy to implement this via a pull request, but wanted to "
            "check if this fits within the project's scope before writing the code.",
            # biopython#4878's reporter, verbatim. Mailman #211.
            "Thanks for the quick reply, yes I can try implementing a solution.",
            "Ok, I'll try and implement (1).",
            "I will try to fix this over the weekend.",
            # terryyin/lizard#487, verbatim; no "I'm" and a verb outside the
            # list, so the owner's "yes, please go ahead" read as an open
            # invitation. Mailman #225.
            "Happy to put that together with tests if you would like it.",
            "Glad to fix this if it helps.",
        ):
            with self.subTest(body=body):
                self.assertEqual(classify_comment(_comment(body)), "claim")

    def test_an_owner_accepting_a_bare_offer_is_a_handover_not_an_invitation(
        self,
    ) -> None:
        # terryyin/lizard#487. Mailman #225.
        thread = [
            _comment("Happy to put that together with tests if you would like it."),
            _comment(
                "Hi @Eljees — yes, please go ahead. Happy to review a PR.",
                login="terryyin",
                association="OWNER",
            ),
        ]
        self.assertEqual(
            classify_thread(thread, maintainers=["terryyin"]), ["claim", "assignment"]
        )

    def test_a_maintainer_happy_to_review_is_not_a_claim(self) -> None:
        self.assertIsNone(classify_comment(_comment("Happy to review a PR.")))

    def test_a_contributor_announcing_their_pull_request_is_a_claim(self) -> None:
        # huggingface/peft#3804, 2026-09-29: three comments from the same
        # contributor, the last naming their pull request, read as no claim,
        # and prescreen passed an issue somebody was actively fixing.
        for body in (
            "Still planning to, yes, this is the one I confirmed on the 25th. "
            "Will have a PR up.",
            "PR is up: #3832. Went with strict_adapter_check as you suggested.",
            "Opened a PR for this: #12",
            "I've opened #3832 for this.",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    classify_comment(_comment(body, association="NONE")), "claim"
                )

    def test_a_claim_under_a_quoted_question_is_still_a_claim(self) -> None:
        # ansible/ansible-lint#4857, 2026-09-29: "I'll work on this" sat under
        # a quote reading "if someone else sees this comment", and the quoted
        # "someone" read the whole comment as a question. Prescreen passed an
        # issue with an open claim.
        body = (
            "> Thanks @Jkhall81! I'm not sure when I'll have time, but in the "
            "meantime, if someone else sees this comment, they would also have "
            "a good starting point.\n\n"
            "@nre-ableton, @Jkhall81; I'll work on this. Thanks for the roadmap."
        )
        self.assertEqual(
            classify_comment(_comment(body, association="NONE")), "claim"
        )

    def test_a_question_elsewhere_does_not_cancel_a_claim(self) -> None:
        # biopython/biopython#5307, 2026-09-12, the reporter asking the
        # maintainer which design to build. "if someone reports it later" read
        # the whole comment as a question, and prescreen passed an issue its
        # reporter was about to implement.
        body = (
            "Hi Peter, I'd like to check one more point before I start, since "
            "it affects how the ambiguous= implementation would work. The one "
            "downside is that we'd be leaving a known bug in place. If someone "
            "reports it later, it would mean a second round of PR and review. "
            "I'll follow whichever option you recommend."
        )
        self.assertEqual(
            classify_comment(_comment(body, association="NONE")), "claim"
        )

    def test_a_quoted_claim_is_not_a_claim(self) -> None:
        body = "> I'll work on this\n\nAny update on this?"
        self.assertIsNone(classify_comment(_comment(body, association="NONE")))

    def test_a_question_about_the_bug_is_not_a_claim(self) -> None:
        for body in (
            "Is anyone working on this?",
            "Any update on this issue?",
            "What version of Python are you on?",
            "I can reproduce this on 3.12 as well",
            "This also breaks for me",
            "PRs welcome",
            "Thanks for the report!",
            "Has this been fixed already?",
            "I am not working on this anymore",
        ):
            with self.subTest(body=body):
                self.assertIsNone(classify_comment(_comment(body)))

    def test_a_maintainer_handing_the_work_over_reads_as_an_assignment(self) -> None:
        for body in (
            "Assigned to you, thanks!",
            "I've assigned this to @someone",
            "Go ahead, all yours",
            "Feel free to open a PR",
            "You can take this one",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    classify_comment(_comment(body, association="MEMBER")),
                    "assignment",
                )

    def test_the_same_words_from_an_outsider_are_not_an_assignment(self) -> None:
        # Only somebody who can actually hand out the work is handing it out.
        self.assertIsNone(classify_comment(_comment("Go ahead, all yours")))

    def test_a_bot_comment_is_never_a_claim(self) -> None:
        comment = _comment("I'm working on this", login="github-actions[bot]")
        comment["user"]["type"] = "Bot"

        self.assertIsNone(classify_comment(comment))


BEETS_6984 = (
    "Marked as `good first issue` for **human** contributors to explore how "
    "plugins handle authentication and token files. Fully automated PRs from "
    "agents may be rejected."
)


class AgentExclusionTests(unittest.TestCase):
    def test_the_beets_wording_reserves_the_issue(self) -> None:
        # GitHub stored the beets maintainer's comment as CONTRIBUTOR.
        self.assertTrue(excludes_agents(_comment(BEETS_6984, association="CONTRIBUTOR")))

    def test_common_refusals_are_read(self) -> None:
        for body in (
            "This one is for human contributors only.",
            "We do not accept AI-generated PRs.",
            "AI-generated pull requests will be closed.",
            "No LLM-written patches, please.",
        ):
            with self.subTest(body=body):
                self.assertTrue(excludes_agents(_comment(body, association="OWNER")))

    def test_a_maintainer_calling_agent_work_slop_reserves_the_issue(self) -> None:
        # pvlib#2864, verbatim. Mailman #198.
        body = (
            "My unique hesitation is that AI bots scrap for new issues and if I "
            "review one more microslop hallucination I drop my career in software"
        )
        self.assertTrue(excludes_agents(_comment(body, association="MEMBER")))

    def test_a_threat_to_ban_drive_by_llm_prs_reserves_the_issue(self) -> None:
        # stanfordnlp/stanza#1651, a collaborator, verbatim. Mailman #245.
        for body in (
            "Anyone else who does a driveby LLM PR with zero interaction with "
            "the maintainers will be banned.",
            "Drive-by AI pull requests will be closed.",
            "AI-generated PRs will get you banned.",
        ):
            with self.subTest(body=body):
                self.assertTrue(
                    excludes_agents(_comment(body, association="COLLABORATOR"))
                )

    def test_a_mentoring_programme_earmark_reserves_the_issue(self) -> None:
        # django-debug-toolbar#2481 and #2482, verbatim. Mailman #194.
        for body in (
            "@VeldaKiara This may be a reasonable djangonaut space ticket.",
            "@VeldaKiara this may be a good djangonaut space ticket",
            "Keeping this one as an Outreachy task.",
        ):
            with self.subTest(body=body):
                self.assertTrue(excludes_agents(_comment(body, association="MEMBER")))

    def test_an_outsider_saying_it_is_not_the_project_speaking(self) -> None:
        self.assertFalse(excludes_agents(_comment(BEETS_6984)))

    def test_ordinary_replies_do_not_match(self) -> None:
        for body in (
            "PRs welcome!",
            "The automated tests fail on Windows.",
            "Human-readable output would be nicer here.",
        ):
            with self.subTest(body=body):
                self.assertFalse(excludes_agents(_comment(body, association="MEMBER")))


class DesignUndecidedTests(unittest.TestCase):
    """Mailman #124: an open design choice in the thread, with no label on it."""

    def test_the_zarr_2706_open_question_blocks(self) -> None:
        thread = [
            _comment("Opening zarr groups on nested fsspec filesystems fails."),
            _comment(
                "The simple case is fixed now. For the remaining work I'm not "
                "sure how we should handle nested filesystems: one option is to "
                "unwrap the inner filesystem, another is to require a URL chain.",
                association="MEMBER",
                login="d-v-b",
            ),
        ]
        rows = design_open_questions(thread)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["author"], "d-v-b")
        self.assertIn("not sure how we should", rows[0]["phrase"])
        self.assertIn("nested filesystems", rows[0]["quote"])

    def test_a_maintainer_with_no_design_blocks(self) -> None:
        # getsentry/responses#744
        thread = [
            _comment("Matchers ignore the query string order."),
            _comment(
                "I haven't decided how this should work. Open to suggestions.",
                association="OWNER",
                login="markstory",
            ),
        ]
        self.assertEqual(len(design_open_questions(thread)), 1)

    def test_a_maintainer_still_thinking_about_it_blocks(self) -> None:
        # huggingface/sentence-transformers#3996, Mailman #252
        thread = [
            _comment("No constructor-based way to set query_length."),
            _comment(
                "My general view is that the outer class should only expose "
                "model-level parameters. For that reason, perhaps it would be "
                "better if it was easier to override the query_length. It's "
                "something to think about well, as it would be 'breaking'.",
                association="MEMBER",
                login="tomaarsen",
            ),
        ]
        self.assertEqual(len(design_open_questions(thread)), 1)

    def test_a_proposed_config_option_without_acceptance_blocks(self) -> None:
        # marimo-team/marimo#6250
        thread = [
            _comment("Autoreload reruns cells I did not touch."),
            _comment(
                "We could add a config option to disable this behaviour.",
                association="CONTRIBUTOR",
                login="mscolnick",
            ),
        ]
        self.assertEqual(len(design_open_questions(thread)), 1)

    def test_common_open_phrasings_block(self) -> None:
        for body in (
            "Not sure what we want here.",
            "Which approach do people prefer?",
            "We need to decide whether this belongs in core.",
            "This needs more discussion before anyone writes code.",
            "Alternatively we could raise instead.",
            "I’d like to hear from other users first.",
            "This probably needs an RFC.",
            "Thanks for the proposal.",
            "Perhaps add a flag for strict mode.",
            # plotly/dash#3968, 2026-09-29: a contributor declining the change
            # for now and polling for demand. Prescreen passed it.
            "I'm inclined to leave the types as is, but I'll leave this open "
            "for now to see if anyone else would like to see a change.",
            # robotframework#5783 and #5747, 2026-09-29: prescreen passed both
            # while the maintainer was still weighing designs. Mailman #205.
            "The first step with getting this done is deciding how/where to "
            "register custom converters. Alternatives: 1. If we add support "
            "for global converters, they should work also with user keywords.",
            "Ping @aaltat, do you have opinions on this? UI side of the feature "
            "should be pretty easy.",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    len(design_open_questions([_comment(body, association="MEMBER")])),
                    1,
                )

    def test_a_maintainer_not_convinced_declines(self) -> None:
        # pypa/pipenv#6715, 2026-09-02, verbatim. Prescreen passed it.
        # Mailman #205.
        body = (
            "I can understand your request, but I am still not convinced its "
            "is a good idea.  Will leave open for now though."
        )
        self.assertEqual(
            len(maintainer_declines([_comment(body, association="MEMBER")])), 1
        )

    def test_an_alternative_in_a_comment_ending_pr_welcome_does_not_block(self) -> None:
        thread = [
            _comment(
                "Alternatively we could special-case empty strings, but the "
                "check in `parse()` is the right place. PR welcome!",
                association="MEMBER",
            )
        ]
        self.assertEqual(design_open_questions(thread), [])

    def test_a_later_maintainer_choice_settles_it(self) -> None:
        thread = [
            _comment(
                "One option is to warn, another would be to raise.",
                association="MEMBER",
            ),
            _comment("Raising seems cleaner to me."),
            _comment("Let's go with raising. Happy to accept a PR.", association="OWNER"),
        ]
        self.assertEqual(design_open_questions(thread), [])

    def test_a_maintainer_liking_the_proposal_settles_it(self) -> None:
        # fonttools#4086: the approval itself names the proposal. Mailman #240.
        thread = [
            _comment(
                "yeah, I like this proposal. We'd use STAT if available as we "
                "currently do, and try to fall back to the fvar instance "
                "matching the target coordinates.",
                association="MEMBER",
            )
        ]
        self.assertEqual(design_open_questions(thread), [])

    def test_a_question_reopened_after_settling_blocks(self) -> None:
        thread = [
            _comment("PRs welcome.", association="MEMBER"),
            _comment(
                "On reflection we need to decide on the API first.",
                association="MEMBER",
            ),
        ]
        self.assertEqual(len(design_open_questions(thread)), 1)

    def test_outsiders_and_bots_neither_open_nor_settle(self) -> None:
        opened = _comment("Which approach should we take?", association="MEMBER")
        outsider_settle = _comment("Let's go with option A, PR welcome.")
        bot = {
            **_comment("Needs discussion.", association="MEMBER"),
            "user": {"login": "stale[bot]", "type": "Bot"},
        }
        self.assertEqual(len(design_open_questions([opened, outsider_settle])), 1)
        self.assertEqual(design_open_questions([_comment("Which approach?")]), [])
        self.assertEqual(design_open_questions([bot]), [])

    def test_ordinary_maintainer_replies_do_not_block(self) -> None:
        for body in (
            "Per RFC 3986 the fragment is optional.",
            "See RFC-7230 section 3.2.",
            "Thanks, I can reproduce this on main.",
            "The option `strict=True` already exists; this is a bug in it.",
            "Confirmed. The fix belongs in `_parse_header`.",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    design_open_questions([_comment(body, association="MEMBER")]), []
                )


class MaintainerDeclinedTests(unittest.TestCase):
    """Mailman #174: a project voice saying no, with no later invitation."""

    def test_a_project_voice_turning_the_report_down_blocks(self) -> None:
        # Both passed prescreen on 2026-09-29.
        for body in (
            # hgrecco/pint#2060
            "I don't think we want to implement this, this complicates things "
            "unecesseraly IMO. Maybe a warning in the docs ?",
            # fsspec/filesystem_spec#1741
            "This is functioning correctly, with behaviour copied from "
            "command-line `cp`.",
            "Works as intended.",
            "That is the expected behavior.",
            "This is not a bug.",
            "We won't fix this in the 1.x line.",
            # conan-io/conan#17492
            "It is not a problem that Conan is raising a NotFoundException.",
            # has2k1/plotnine#917
            "This is expected because at moment plotnine geoms are not yet "
            "aware of their orientation.",
            # davidhalter/jedi#2058
            "This is the kind of bug that is unlikely to be fixed here.",
            # huggingface/huggingface_hub#2742
            "not something we want to do at this stage no",
            "We don't want to raise an exception at this stage for the reason "
            "explained above.",
        ):
            with self.subTest(body=body):
                rows = maintainer_declines([_comment(body, association="MEMBER")])
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0]["quote"])

    def test_an_outsider_saying_no_is_an_opinion(self) -> None:
        self.assertEqual(maintainer_declines([_comment("This is not a bug.")]), [])

    def test_a_later_invitation_overrides_the_decline(self) -> None:
        thread = [
            _comment("Works as intended.", association="MEMBER"),
            _comment("Fair point, you're right. PR welcome!", association="OWNER"),
        ]
        self.assertEqual(maintainer_declines(thread), [])

    def test_a_confirmed_bug_is_not_a_decline(self) -> None:
        for body in (
            "Thanks, I agree that this is a bug.",
            "This is a bug, the expected behavior is to keep the order.",
        ):
            with self.subTest(body=body):
                self.assertEqual(
                    maintainer_declines([_comment(body, association="MEMBER")]), []
                )


class ReadClaimsTests(unittest.TestCase):
    def _run(self, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "issue.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "reference": {
                        "owner": "openai",
                        "repository": "openai-agents-python",
                        "number": 4775,
                        "url": (
                            "https://github.com/openai/openai-agents-python"
                            "/issues/4775"
                        ),
                    },
                }
            ),
            encoding="utf-8",
        )
        return root

    def test_a_claim_in_the_comments_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": []},
                    [
                        _comment("Thanks for the report!", association="MEMBER"),
                        _comment("I'd like to work on this issue."),
                    ],
                ),
            )
            # Read back inside the block: the record is on disk only as long
            # as the temporary directory is.
            self.assertEqual(load_claims(root), record)
            self.assertTrue((root / CLAIMS_FILENAME).is_file())

        self.assertTrue(record["success"])
        self.assertEqual(record["repository"], "openai/openai-agents-python")
        self.assertEqual(record["issue_number"], 4775)
        self.assertEqual(record["comments_read"], 2)
        self.assertEqual(len(record["claims"]), 1)
        self.assertEqual(record["claims"][0]["author"], "someone")
        self.assertEqual(record["assignments"], [])
        self.assertEqual(record["assignees"], [])
        self.assertIn("work on this issue", record["claims"][0]["quote"])

    def test_a_claim_in_the_issue_body_is_recorded(self) -> None:
        # domokane/FinancePy#267: the report carries the diff and nobody has
        # commented, and the reporter has still claimed the work.
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {
                        "number": 267,
                        "assignees": [],
                        "user": {"login": "reporter"},
                        "author_association": "CONTRIBUTOR",
                        "body": (
                            "Proposed correction in `bond_zero.py`: "
                            "```diff -  md = dd / fp * 10000 +  md = dd / fp```"
                        ),
                    },
                    [],
                ),
            )

        self.assertTrue(record["success"])
        self.assertEqual(record["comments_read"], 0)
        self.assertEqual(len(record["claims"]), 1)
        self.assertEqual(record["claims"][0]["author"], "reporter")
        self.assertIn("Proposed correction", record["claims"][0]["quote"])

    def test_an_assigned_issue_records_its_assignee(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": [{"login": "maintainer"}]}, []
                ),
            )

        self.assertTrue(record["success"])
        self.assertEqual(record["assignees"], ["maintainer"])

    def test_a_maintainer_reply_is_recorded_as_an_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": []},
                    [
                        _comment("I'd like to work on this."),
                        _comment("Go ahead, all yours", association="MEMBER"),
                    ],
                ),
            )

        self.assertEqual(len(record["claims"]), 1)
        self.assertEqual(len(record["assignments"]), 1)
        self.assertEqual(record["assignments"][0]["author"], "someone")

    def test_a_clean_issue_records_no_claim_and_still_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": []},
                    [_comment("I can reproduce this on 3.12 as well")],
                ),
            )

        self.assertTrue(record["success"])
        self.assertEqual(record["claims"], [])
        self.assertEqual(record["comments_read"], 1)
        self.assertIn("no claim", render_claims(record).lower())

    def test_an_unreadable_issue_does_not_pass_as_a_clean_one(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(root, executable="gh", execute=_failing)

        self.assertFalse(record["success"])
        self.assertIn("detail", record)

    def test_a_run_with_no_captured_issue_cannot_be_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = read_claims(Path(temporary), executable="gh", execute=_failing)

        self.assertFalse(record["success"])
        self.assertIn("issue", record["detail"])

    def test_the_rendered_record_names_the_claimant_and_the_quote(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": []},
                    [_comment("I'd like to work on this issue.")],
                ),
            )
        rendered = render_claims(record)

        self.assertIn("someone", rendered)
        self.assertIn("work on this issue", rendered)


class InvitationTests(unittest.TestCase):
    """A maintainer asking for the pull request, told apart from a handover.

    The shortlist ranks on this, so the recogniser lives beside the claim
    rules rather than in a second copy of them. See
    https://github.com/wolfgang-aura/Mailman/issues/102.
    """

    def test_the_invitation_phrasings_read_as_invitations(self) -> None:
        for body in (
            "PRs welcome!",
            "Pull requests are welcome.",
            "Contributions welcome.",
            "Happy to accept a PR for this.",
            "We'd gladly review a pull request.",
            "Feel free to open a PR.",
            "A PR would be appreciated.",
        ):
            with self.subTest(body=body):
                self.assertTrue(
                    is_maintainer_invitation(_comment(body, association="MEMBER"))
                )

    def test_the_same_words_from_an_outsider_are_not_an_invitation(self) -> None:
        for association in ("NONE", "CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR"):
            with self.subTest(association=association):
                self.assertFalse(
                    is_maintainer_invitation(
                        _comment("PRs welcome!", association=association)
                    )
                )

    def test_a_bot_never_invites(self) -> None:
        comment = _comment("PRs welcome!", association="MEMBER", login="stale[bot]")
        comment["user"]["type"] = "Bot"

        self.assertFalse(is_maintainer_invitation(comment))

    def test_a_reply_about_the_bug_is_not_an_invitation(self) -> None:
        self.assertFalse(
            is_maintainer_invitation(
                _comment("I can reproduce this on main.", association="MEMBER")
            )
        )

    def test_feel_free_answers_a_claim_as_a_handover_and_nobody_as_an_invitation(
        self,
    ) -> None:
        answered = [
            _comment("Can I take this?"),
            _comment("Feel free to open a PR.", association="MEMBER"),
        ]
        unasked = [
            _comment("Same here on 3.12."),
            _comment("Feel free to open a PR.", association="MEMBER"),
        ]

        self.assertEqual(classify_thread(answered), ["claim", "assignment"])
        self.assertEqual(classify_thread(unasked), [None, "invitation"])

    def test_the_record_keeps_the_invitation_and_when_a_maintainer_last_wrote(
        self,
    ) -> None:
        maintainer = _comment("PRs welcome.", association="MEMBER", login="owner")
        maintainer["created_at"] = "2026-09-10T00:00:00Z"
        with tempfile.TemporaryDirectory() as temporary:
            root = ReadClaimsTests._run(self, Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {
                        "number": 4775,
                        "assignees": [],
                        "author_association": "NONE",
                        "created_at": "2026-09-01T00:00:00Z",
                    },
                    [_comment("Same here."), maintainer],
                ),
            )

        self.assertEqual(record["claims"], [])
        self.assertEqual(record["assignments"], [])
        self.assertEqual(len(record["invitations"]), 1)
        self.assertEqual(record["invitations"][0]["author"], "owner")
        self.assertEqual(record["maintainer_touched_at"], "2026-09-10T00:00:00Z")
        self.assertEqual(record["issue_created_at"], "2026-09-01T00:00:00Z")
        self.assertIn("asking for a pull request", render_claims(record))


if __name__ == "__main__":
    unittest.main()


class ListedMaintainerTests(unittest.TestCase):
    """A maintainer GitHub reports as CONTRIBUTOR. Mailman #203.

    marimo's mscolnick has a private membership. Only the login set the
    repository screen recorded tells him apart from an outsider.
    """

    def test_a_listed_contributor_invites(self) -> None:
        comment = _comment(
            "PRs welcome!", association="CONTRIBUTOR", login="mscolnick"
        )
        self.assertFalse(is_maintainer_invitation(comment))
        self.assertTrue(
            is_maintainer_invitation(comment, maintainers={"mscolnick"})
        )

    def test_read_claims_counts_a_listed_reply_only_with_the_set(self) -> None:
        issue = {
            "number": 4775,
            "assignees": [],
            "author_association": "NONE",
            "user": {"login": "reporter"},
        }
        comments = [
            _comment(
                "Thanks, confirmed.", association="CONTRIBUTOR", login="mscolnick"
            )
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = ReadClaimsTests()._run(Path(temporary))
            before = read_claims(
                root, executable="gh", execute=_FakeGh(issue, comments)
            )
            after = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(issue, comments),
                maintainers={"mscolnick"},
            )

        self.assertFalse(before["maintainer_replied"])
        self.assertTrue(after["maintainer_replied"])
        self.assertFalse(after["reporter_is_maintainer"])


class TriageFieldsTests(unittest.TestCase):
    """What the record says about who reported the issue and who answered.
    See https://github.com/wolfgang-aura/Mailman/issues/88."""

    def _run(self, root: Path) -> Path:
        return ReadClaimsTests._run(self, root)

    def test_an_outside_report_with_no_maintainer_reply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {
                        "number": 4775,
                        "assignees": [],
                        "state": "open",
                        "closed_at": None,
                        "author_association": "NONE",
                    },
                    [_comment("Reverified, still reproduces.")],
                ),
            )
            self.assertEqual(record["reporter_association"], "NONE")
            self.assertFalse(record["maintainer_replied"])
            self.assertIsNone(record["issue_closed_at"])

    def test_a_member_reply_and_a_close_are_both_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {
                        "number": 4775,
                        "assignees": [],
                        "state": "closed",
                        "closed_at": "2026-09-08T09:27:46Z",
                        "author_association": "NONE",
                    },
                    [_comment("Fixed in dc4e314.", association="OWNER")],
                ),
            )
            self.assertTrue(record["maintainer_replied"])
            self.assertEqual(record["issue_state"], "closed")
            self.assertEqual(record["issue_closed_at"], "2026-09-08T09:27:46Z")


class OfferReplyTests(unittest.TestCase):
    """A maintainer's answer to an ask-first offer is read from the thread.
    See https://github.com/wolfgang-aura/Mailman/issues/138."""

    ISSUE = {"number": 4775, "assignees": [], "state": "open",
             "closed_at": None, "author_association": "NONE"}

    def _read(self, root: Path, comments: list[dict], *, offer: bool = True) -> dict:
        ReadClaimsTests._run(self, root)
        if offer:
            (root / "handoff-offer.json").write_text(
                json.dumps({"kind": "issue-comment", "offer": True,
                            "prepared_at": "2026-09-10T00:00:00+00:00"}),
                encoding="utf-8",
            )
        return read_claims(root, executable="gh", execute=_FakeGh(self.ISSUE, comments))

    @staticmethod
    def _at(comment: dict, stamp: str) -> dict:
        return {**comment, "created_at": stamp}

    def test_only_a_maintainer_reply_after_the_offer_counts(self) -> None:
        bot = _comment("Stale.", association="MEMBER", login="stale[bot]")
        bot["user"]["type"] = "Bot"
        with tempfile.TemporaryDirectory() as temporary:
            record = self._read(Path(temporary), [
                self._at(_comment("Earlier note.", association="OWNER", login="old"),
                         "2026-09-09T23:59:59Z"),
                self._at(_comment("+1, same here.", association="CONTRIBUTOR"),
                         "2026-09-10T01:00:00Z"),
                self._at(bot, "2026-09-10T02:00:00Z"),
                self._at(_comment("Yes, please open a PR.", association="MEMBER",
                                  login="keeper"), "2026-09-11T08:00:00Z"),
            ])
        self.assertEqual(record["offer_prepared_at"], "2026-09-10T00:00:00+00:00")
        self.assertEqual([row["author"] for row in record["offer_replies"]], ["keeper"])
        self.assertEqual(record["offer_replies"][0]["association"], "MEMBER")

    def test_no_offer_means_no_offer_replies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record = self._read(Path(temporary), [
                _comment("Yes, please open a PR.", association="OWNER"),
            ], offer=False)
        self.assertNotIn("offer_replies", record)


def _labelled(name: str, login: str, *, kind: str = "User") -> dict:
    return {
        "event": "labeled",
        "actor": {"login": login, "type": kind},
        "label": {"name": name},
        "created_at": "2026-09-20T14:47:19Z",
    }


class MaintainerLabelTests(unittest.TestCase):
    """A maintainer who triages by labelling has triaged the issue.

    securo-finance/securo#972 carried `prio:high` and `risk:medium`, both added
    by a committer, and no comment. The untriaged-issue question fired anyway.
    See https://github.com/wolfgang-aura/Mailman/issues/139.
    """

    def _read(self, root: Path, timeline: list[dict]) -> dict:
        ReadClaimsTests._run(self, root)
        return read_claims(
            root,
            executable="gh",
            execute=_FakeGh(
                {
                    "number": 972,
                    "assignees": [],
                    "state": "open",
                    "author_association": "NONE",
                    "user": {"login": "reporter", "type": "User"},
                },
                [],
                timeline=timeline,
            ),
        )

    def test_a_label_a_maintainer_added_counts_as_triage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = self._read(
                root,
                [
                    _labelled("prio:high", "tassionoronha"),
                    _labelled("risk:medium", "tassionoronha"),
                ],
            )
            self.assertEqual(
                record["maintainer_labelled"],
                [
                    {
                        "labels": ["prio:high", "risk:medium"],
                        "actor": "tassionoronha",
                        "at": "2026-09-20T14:47:19Z",
                    }
                ],
            )
            self.assertIsNone(triage_warning(root))

    def test_a_label_the_reporter_added_is_not_triage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = self._read(root, [_labelled("bug", "reporter")])
            self.assertEqual(record["maintainer_labelled"], [])
            self.assertIsNotNone(triage_warning(root))

    def test_a_label_a_bot_added_is_not_triage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = self._read(
                root, [_labelled("needs-triage", "github-actions[bot]", kind="Bot")]
            )
            self.assertEqual(record["maintainer_labelled"], [])
            self.assertIsNotNone(triage_warning(root))


class PullRequestReferenceTests(unittest.TestCase):
    """Every way a thread names the pull request that already fixes the issue.

    https://github.com/wolfgang-aura/Mailman/issues/98
    """

    def test_reads_a_bare_number_a_slug_and_a_url(self) -> None:
        found = pull_request_references(
            [
                "Draft implementation: "
                "[#12775](https://github.com/deepset-ai/haystack/pull/12775)",
                "See also getsentry/sentry-python#4001 and #22722.",
            ],
            repository="deepset-ai/haystack",
            exclude=[("deepset-ai/haystack", 12777)],
        )

        self.assertEqual(
            [(row["repository"], row["number"]) for row in found],
            [
                ("deepset-ai/haystack", 12775),
                ("getsentry/sentry-python", 4001),
                ("deepset-ai/haystack", 22722),
            ],
        )
        self.assertIn("12775", found[0]["text"])

    def test_the_issues_own_number_is_never_resolved(self) -> None:
        found = pull_request_references(
            ["This is a duplicate of #1497, filed as #1497 again."],
            repository="python-jsonschema/jsonschema",
            exclude=[("python-jsonschema/jsonschema", 1497)],
        )

        self.assertEqual(found, [])

    def test_the_same_pull_request_named_twice_is_one_reference(self) -> None:
        found = pull_request_references(
            [
                "Fixed by #22722.",
                "PR #22722 was merged on 2026-08-05.",
                "https://github.com/PrefectHQ/prefect/pull/22722",
            ],
            repository="PrefectHQ/prefect",
        )

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["number"], 22722)

    def test_a_sibling_repository_shorthand_keeps_its_repository(self) -> None:
        # docling#2623: "docling-core#466 has been open since January" named
        # the open fix, and the screen resolved docling#466 instead. Mailman #235.
        found = pull_request_references(
            [
                "docling-core#466 has been open since January.",
                "Tried PR#12, issue#13 and docling#14.",
            ],
            repository="docling-project/docling",
        )

        self.assertEqual(
            [(row["repository"], row["number"]) for row in found],
            [
                ("docling-project/docling-core", 466),
                ("docling-project/docling", 12),
                ("docling-project/docling", 13),
                ("docling-project/docling", 14),
            ],
        )

    def test_the_count_is_capped_because_each_reference_is_a_gh_call(self) -> None:
        body = " ".join(f"#{index}" for index in range(100, 130))
        self.assertEqual(len(pull_request_references([body], repository="a/b")), 10)


class ReferenceRecordTests(unittest.TestCase):
    """What `read_claims` writes down for `prescreen` to resolve."""

    def _run(self, root: Path) -> Path:
        return ReadClaimsTests._run(self, root)

    def test_the_body_and_the_comments_are_both_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {
                        "number": 4775,
                        "assignees": [],
                        "body": "Draft implementation: #4800",
                    },
                    [_comment("Also fixed by openai/openai-python#900.")],
                ),
            )

        self.assertEqual(
            [(row["repository"], row["number"]) for row in record["references"]],
            [
                ("openai/openai-agents-python", 4800),
                ("openai/openai-python", 900),
            ],
        )

    def test_a_cross_referenced_pull_request_is_read_off_the_timeline(self) -> None:
        # python-jsonschema/jsonschema#1497: the one comment says "the
        # issue/PR in referencing" and names no number. GitHub linked it.
        with tempfile.TemporaryDirectory() as temporary:
            root = self._run(Path(temporary))
            record = read_claims(
                root,
                executable="gh",
                execute=_FakeGh(
                    {"number": 4775, "assignees": [], "body": "No numbers here."},
                    [_comment("Please look at the issue/PR in referencing.")],
                    [
                        _cross_reference(
                            "https://github.com/python-jsonschema/referencing/issues/366"
                        ),
                        _cross_reference(
                            "https://github.com/python-jsonschema/referencing/pull/367"
                        ),
                    ],
                ),
            )

        self.assertIn(
            ("python-jsonschema/referencing", 367),
            [(row["repository"], row["number"]) for row in record["references"]],
        )


class ReportedFixedTests(unittest.TestCase):
    """Mailman #236: anyone saying the bug is gone on main is worth a warning."""

    @staticmethod
    def _comment(body, association="NONE", created="2026-09-01T00:00:00Z"):
        return {
            "body": body,
            "author_association": association,
            "user": {"login": "someone", "type": "User"},
            "created_at": created,
        }

    def test_no_longer_reproduces_on_main_is_reported(self):
        from mailman.claims import reported_fixed

        # pylint#10032, 2026-09-09.
        for body in (
            "This no longer reproduces on current `main` (pylint 4.1.0-dev0, "
            "astroid 4.2.0b5). It may be worth closing.",
            "I can't reproduce this on master anymore.",
            "Looks like this was fixed in the latest release.",
            "This doesn't happen with the latest version.",
        ):
            with self.subTest(body=body):
                self.assertIsNotNone(reported_fixed([self._comment(body)]))

    def test_a_later_still_happening_reply_wins(self):
        from mailman.claims import reported_fixed

        thread = [
            self._comment("This no longer reproduces on main."),
            self._comment(
                "It still happens for me on main, see the traceback.",
                created="2026-09-02T00:00:00Z",
            ),
        ]
        self.assertIsNone(reported_fixed(thread))

    def test_an_ordinary_report_is_not_fixed(self):
        from mailman.claims import reported_fixed

        for body in (
            "I can't reproduce this on 3.11 but it fails on 3.12.",
            "This still reproduces on main.",
            "Fixed my config, but the crash remains.",
        ):
            with self.subTest(body=body):
                self.assertIsNone(reported_fixed([self._comment(body)]))


class MaintainerDisputeTests(unittest.TestCase):
    """Mailman #150: a maintainer reply is triage only when it does not dispute."""

    @staticmethod
    def _comment(body, association="MEMBER"):
        return {
            "body": body,
            "author_association": association,
            "user": {"login": "someone", "type": "User"},
            "created_at": "2026-09-01T00:00:00Z",
        }

    def test_cannot_reproduce_is_a_dispute(self):
        from mailman.claims import maintainer_dispute

        quote = maintainer_dispute(
            [self._comment("Thanks. I could not reproduce this on main.")]
        )
        self.assertEqual(quote, "I could not reproduce this on main.")

    def test_tried_to_reproduce_but_could_not_is_a_dispute(self):
        # jedi#2077's owner split the verb, and prescreen read it as triage.
        # Mailman #210.
        from mailman.claims import maintainer_dispute

        for body in (
            "I have tried to reproduce this, but couldn't. Would have to look deeper inside.",
            "Tried to replicate it on 3.12 but wasn't able to.",
            "I tried reproducing with your script but failed.",
        ):
            with self.subTest(body=body):
                self.assertIsNotNone(maintainer_dispute([self._comment(body)]))
        self.assertIsNone(
            maintainer_dispute(
                [self._comment("I tried to reproduce this but it only fails on Windows, confirmed.")]
            )
        )

    def test_liking_the_proposal_is_not_a_dispute(self):
        # fonttools#4086. Mailman #240.
        from mailman.claims import maintainer_dispute

        self.assertIsNone(
            maintainer_dispute(
                [
                    self._comment(
                        "yeah, I like this proposal. We'd use STAT if available "
                        "and fall back to the fvar instance. @simoncozens do you "
                        "plan to work on a PR?"
                    )
                ]
            )
        )

    def test_a_later_confirmation_ends_the_dispute(self):
        from mailman.claims import maintainer_dispute

        self.assertIsNone(
            maintainer_dispute(
                [
                    self._comment("I cannot reproduce this."),
                    self._comment("Thanks for the script", association="NONE"),
                    self._comment("Ok, with that script I can reproduce it."),
                ]
            )
        )

    def test_checking_whether_it_reproduces_is_not_a_confirmation(self):
        # PrefectHQ/prefect#22334: the latest word promised a check. Mailman #230.
        from mailman.claims import maintainer_dispute

        for body in (
            "Ah, gotcha, I'll check to see if I can reproduce the issue.",
            "Let me see whether we can reproduce this on main.",
            # prefect#22314 pointed the reporter at the docs.
            "hi @cheepon - have you checked out the database maintenance docs?",
        ):
            with self.subTest(body=body):
                self.assertIsNotNone(maintainer_dispute([self._comment(body)]))

    def test_an_outsider_saying_works_for_me_is_not_a_dispute(self):
        from mailman.claims import maintainer_dispute

        self.assertIsNone(
            maintainer_dispute([self._comment("Works for me", association="NONE")])
        )

    def test_a_translation_platform_redirect_is_a_dispute(self):
        # django-debug-toolbar#2329, verbatim. Mailman #194.
        from mailman.claims import maintainer_dispute

        self.assertIsNotNone(maintainer_dispute([self._comment(
            "@domingues would you be willing to adjust this in Transifex? It's "
            "a bit of work, but that's where we manage translations."
        )]))

    def test_a_format_limitation_and_a_request_to_show_state_are_disputes(self):
        # biopython#5101 and s3fs#999, verbatim. Mailman #195.
        from mailman.claims import maintainer_dispute

        for body in (
            "I think your 'better solution' will have too many false positives "
            "(words wrongly stuck together).\n\nI do agree that this is a "
            "limitation of the GenBank file format.",
            "Could you please show the contents of fs.dircache after each call? "
            "That must be where things are changing.",
        ):
            with self.subTest(body=body[:30]):
                self.assertIsNotNone(maintainer_dispute([self._comment(body)]))

    def test_a_request_to_retry_on_the_latest_release_is_not_triage(self):
        # kombu#2291, unanswered for 518 days, ranked engaged. Mailman #190.
        from mailman.claims import maintainer_dispute

        self.assertIsNotNone(
            maintainer_dispute([self._comment(
                "can you please try latest release of kombu with the latest "
                "release of celery and report back?"
            )])
        )
        self.assertIsNotNone(
            maintainer_dispute([self._comment(
                "Could you provide a minimal reproducer?"
            )])
        )
        self.assertIsNone(
            maintainer_dispute([
                self._comment("Could you provide a minimal reproducer?"),
                self._comment("here it is", association="NONE"),
                self._comment("Thanks, confirmed on main."),
            ])
        )

    def test_works_as_designed_is_a_dispute(self):
        from mailman.claims import maintainer_dispute

        self.assertIsNotNone(
            maintainer_dispute([self._comment("This works as designed.")])
        )

    def test_a_stated_reason_for_not_doing_it_is_a_decline(self):
        # docling#3528, cau-git (MEMBER). Mailman #234.
        from mailman.claims import maintainer_declines

        self.assertTrue(
            maintainer_declines([
                self._comment(
                    "@1313e the rationale of docling not choosing sides between "
                    "`opencv-python-headless` and `opencv-python` is that one or "
                    "the other must be preferred. More control is possible with "
                    "`docling-slim`."
                )
            ])
        )
        self.assertFalse(
            maintainer_declines([
                self._comment("The reason for the crash is not obvious yet.")
            ])
        )


class RemarksElsewhereTests(unittest.TestCase):
    """Mailman #187: pyinstaller#9224 held the maintainers' view of #9121."""

    def test_a_maintainer_remark_in_a_cross_referencing_issue_is_read(self) -> None:
        def row(body, association, login):
            return {
                "body": body,
                "author_association": association,
                "user": {"login": login, "type": "User"},
                "created_at": "2025-08-30T10:40:44Z",
                "html_url": "https://github.com/pyinstaller/pyinstaller/issues/9224#c1",
            }

        timeline = [
            {"event": "cross-referenced", "source": {"issue": {
                "html_url": "https://github.com/pyinstaller/pyinstaller/issues/9224"}}},
            {"event": "cross-referenced", "source": {"issue": {
                "html_url": "https://github.com/other/project/issues/5"}}},
            {"event": "cross-referenced", "source": {"issue": {
                "html_url": "https://github.com/pyinstaller/pyinstaller/pull/9300",
                "pull_request": {}}}},
        ]
        elsewhere = {
            "repos/pyinstaller/pyinstaller/issues/9224/comments": [
                row("But perhaps we should always use `--best` in our [fixed set "
                    "of `upx` parameters](https://github.com/pyinstaller/"
                    "pyinstaller/blob/4f2790a717b9cfe93af58a93c87f9ccb7c5534d1/"
                    "PyInstaller/building/utils.py#L281-L292) - this way we "
                    "could probably also side-step "
                    "https://github.com/pyinstaller/pyinstaller/issues/9121 "
                    "(without having explicit fallbacks).",
                    "MEMBER", "rokm"),
                row("Same with #9121 here.", "NONE", "someone"),
                row("Unrelated to that issue.", "MEMBER", "bwoodsend"),
            ]
        }
        gh = _FakeGh({}, [], timeline=timeline, elsewhere=elsewhere)

        def api(path, **query):
            return json.loads(gh(["gh", "api", path]).stdout)

        from mailman.claims import remarks_elsewhere

        found = remarks_elsewhere(
            timeline, api, repository="pyinstaller/pyinstaller", number=9121
        )

        self.assertEqual([row["author"] for row in found], ["rokm"])
        self.assertIn("without having explicit fallbacks", found[0]["quote"])
        self.assertIn("always use `--best`", found[0]["quote"])
        self.assertNotIn("utils.py", found[0]["quote"])
        self.assertEqual(
            found[0]["source"], "https://github.com/pyinstaller/pyinstaller/issues/9224"
        )
        self.assertEqual(
            gh.asked, ["repos/pyinstaller/pyinstaller/issues/9224/comments"]
        )
