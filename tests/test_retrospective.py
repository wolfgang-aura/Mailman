from __future__ import annotations

import unittest

from mailman.knowledge.retrospective import (
    RETROSPECTIVE_SECTIONS,
    UNRECORDED,
    Observation,
    Retrospective,
    RunVersions,
    render_retrospective,
)
from mailman.knowledge.taxonomy import (
    CHANNEL_WEIGHTS,
    OBSERVED_EVIDENCE_WEIGHT,
    EvidenceChannel,
    Outcome,
    PatternCategory,
    Scope,
    channel_weight,
    is_observed,
    strongest_channel,
)


def sample_retrospective() -> Retrospective:
    return Retrospective(
        run_id="20260901T201921Z-0b85ed",
        repository="https://github.com/example/project.git",
        issue="https://github.com/example/project/issues/9",
        base_commit="a" * 40,
        run_status="READY_FOR_HUMAN_REVIEW",
        versions=RunVersions(
            skill_version="unversioned",
            primary_prompt_version="sha256:" + "b" * 64,
            review_prompt_version="sha256:" + "c" * 64,
            orchestrator_version="0.1.0",
            primary_model="codex-test-model",
            reviewer_model="claude-test-model",
        ),
        run_facts={"review_cycles": 1, "verification_runs": 2},
    )


class ChannelWeightTests(unittest.TestCase):
    def test_agent_self_report_is_the_weakest_channel(self) -> None:
        weakest = min(CHANNEL_WEIGHTS, key=channel_weight)
        self.assertEqual(weakest, EvidenceChannel.AGENT_RETROSPECTIVE)

    def test_a_failing_test_outweighs_an_agent_opinion(self) -> None:
        self.assertGreater(
            channel_weight(EvidenceChannel.AUTOMATED_VERIFICATION),
            channel_weight(EvidenceChannel.AGENT_RETROSPECTIVE),
        )
        self.assertGreater(
            channel_weight(EvidenceChannel.MAINTAINER_FEEDBACK),
            channel_weight(EvidenceChannel.REVIEWER_FINDING),
        )

    def test_only_machine_and_human_channels_count_as_observed(self) -> None:
        observed = {
            channel for channel in EvidenceChannel if is_observed(channel)
        }
        self.assertEqual(
            observed,
            {
                EvidenceChannel.MAINTAINER_FEEDBACK,
                EvidenceChannel.POST_MERGE_REGRESSION,
                EvidenceChannel.AUTOMATED_VERIFICATION,
                EvidenceChannel.HUMAN_REVIEW,
                EvidenceChannel.PRIMARY_AGENT_FAILURE,
            },
        )
        self.assertFalse(is_observed(EvidenceChannel.REVIEWER_FINDING))
        self.assertFalse(is_observed(EvidenceChannel.AGENT_RETROSPECTIVE))

    def test_every_channel_has_a_weight(self) -> None:
        self.assertEqual(set(CHANNEL_WEIGHTS), set(EvidenceChannel))

    def test_strongest_channel_of_nothing_is_none(self) -> None:
        self.assertIsNone(strongest_channel([]))
        self.assertEqual(
            strongest_channel(
                [
                    EvidenceChannel.AGENT_RETROSPECTIVE,
                    EvidenceChannel.HUMAN_REVIEW,
                    EvidenceChannel.REVIEWER_FINDING,
                ]
            ),
            EvidenceChannel.HUMAN_REVIEW,
        )


class ObservationTests(unittest.TestCase):
    def test_observation_carries_its_channel_weight(self) -> None:
        observation = Observation(
            observation_id="OBS-001",
            channel=EvidenceChannel.AUTOMATED_VERIFICATION,
            category=PatternCategory.VERIFICATION,
            outcome=Outcome.FAILURE,
            summary="Verification failed after the primary stage.",
        )
        self.assertEqual(observation.weight, OBSERVED_EVIDENCE_WEIGHT + 1)

    def test_summary_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "summary is required"):
            Observation(
                observation_id="OBS-001",
                channel=EvidenceChannel.AGENT_RETROSPECTIVE,
                category=PatternCategory.REVIEW,
                outcome=Outcome.SUCCESS,
                summary="   ",
            )

    def test_observation_id_is_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "observation_id"):
            Observation(
                observation_id="../escape",
                channel=EvidenceChannel.AGENT_RETROSPECTIVE,
                category=PatternCategory.REVIEW,
                outcome=Outcome.SUCCESS,
                summary="ok",
            )

    def test_round_trip_preserves_every_field(self) -> None:
        observation = Observation(
            observation_id="OBS-002",
            channel=EvidenceChannel.REVIEWER_FINDING,
            category=PatternCategory.TEST_DESIGN,
            outcome=Outcome.MIXED,
            summary="The reviewer asked for a regression test.",
            assumption="An existing test already covered the branch.",
            detail="The reviewer named the uncovered branch.",
            scope=Scope.ECOSYSTEM,
            evidence=("agent-executions/0002-reviewer.json",),
            candidate_guidance="Name the covering test before claiming coverage.",
        )
        restored = Observation.from_dict(observation.to_dict())
        self.assertEqual(restored, observation)


class RetrospectiveTests(unittest.TestCase):
    def test_observation_ids_are_sequential(self) -> None:
        retrospective = sample_retrospective()
        first = retrospective.add_observation(
            channel=EvidenceChannel.AUTOMATED_VERIFICATION,
            category=PatternCategory.VERIFICATION,
            outcome=Outcome.SUCCESS,
            summary="Verification passed twice.",
        )
        second = retrospective.add_observation(
            channel=EvidenceChannel.REVIEWER_FINDING,
            category=PatternCategory.REVIEW,
            outcome=Outcome.FAILURE,
            summary="The reviewer requested one revision.",
        )
        self.assertEqual(first.observation_id, "OBS-001")
        self.assertEqual(second.observation_id, "OBS-002")

    def test_counts_and_strongest_evidence(self) -> None:
        retrospective = sample_retrospective()
        retrospective.add_observation(
            channel=EvidenceChannel.AGENT_RETROSPECTIVE,
            category=PatternCategory.SCOPE_CONTROL,
            outcome=Outcome.FAILURE,
            summary="Read files unrelated to the issue.",
        )
        retrospective.add_observation(
            channel=EvidenceChannel.AUTOMATED_VERIFICATION,
            category=PatternCategory.VERIFICATION,
            outcome=Outcome.FAILURE,
            summary="The unit suite failed once.",
        )
        self.assertEqual(
            retrospective.strongest_evidence(),
            EvidenceChannel.AUTOMATED_VERIFICATION,
        )
        self.assertEqual(
            retrospective.channel_counts(),
            {"AGENT_RETROSPECTIVE": 1, "AUTOMATED_VERIFICATION": 1},
        )
        self.assertEqual(
            retrospective.category_counts(),
            {"SCOPE_CONTROL": 1, "VERIFICATION": 1},
        )

    def test_round_trip_preserves_versions_and_observations(self) -> None:
        retrospective = sample_retrospective()
        retrospective.add_observation(
            channel=EvidenceChannel.PRIMARY_AGENT_FAILURE,
            category=PatternCategory.TOOL_USAGE,
            outcome=Outcome.FAILURE,
            summary="The primary agent exited zero without a report.",
            assumption="A zero exit code means the task was completed.",
            evidence=("agent-executions/0001-primary.json",),
        )
        restored = Retrospective.from_dict(retrospective.to_dict())
        self.assertEqual(restored.to_dict(), retrospective.to_dict())
        self.assertEqual(restored.versions.orchestrator_version, "0.1.0")
        self.assertEqual(restored.observations[0].weight, 4)

    def test_missing_versions_are_recorded_not_omitted(self) -> None:
        versions = RunVersions()
        self.assertEqual(versions.skill_version, UNRECORDED)
        self.assertEqual(versions.to_dict()["review_prompt_version"], UNRECORDED)


class TemplateTests(unittest.TestCase):
    def test_every_required_question_has_a_section(self) -> None:
        keys = {section.key for section in RETROSPECTIVE_SECTIONS}
        self.assertEqual(
            keys,
            {
                "successes",
                "failures",
                "incorrect_assumptions",
                "reviewer_findings",
                "verification_failures",
                "human_corrections",
                "maintainer_feedback",
                "unnecessary_work",
                "missing_investigation",
                "missing_tests",
                "tooling_and_environment",
                "reusable_lessons",
            },
        )

    def test_render_includes_facts_seeded_observations_and_open_questions(
        self,
    ) -> None:
        retrospective = sample_retrospective()
        retrospective.add_observation(
            channel=EvidenceChannel.AUTOMATED_VERIFICATION,
            category=PatternCategory.VERIFICATION,
            outcome=Outcome.FAILURE,
            summary="Verification failed after the revision stage.",
            evidence=("commands/0002.json",),
        )
        rendered = render_retrospective(retrospective)

        self.assertIn("# Retrospective for run 20260901T201921Z-0b85ed", rendered)
        self.assertIn("| Orchestrator | `0.1.0` |", rendered)
        self.assertIn("| review_cycles | 1 |", rendered)
        self.assertIn("OBS-001. Verification failed after the revision stage.", rendered)
        self.assertIn("`commands/0002.json`", rendered)
        for section in RETROSPECTIVE_SECTIONS:
            self.assertIn(f"### {section.title}", rendered)
        self.assertTrue(rendered.endswith("\n"))

    def test_render_without_observations_says_so(self) -> None:
        rendered = render_retrospective(sample_retrospective())
        self.assertIn("Mailman observed nothing worth recording", rendered)


if __name__ == "__main__":
    unittest.main()
