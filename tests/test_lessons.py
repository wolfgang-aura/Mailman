from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mailman.knowledge.lessons import (
    MINIMUM_VALIDATION_RUNS,
    LessonEntry,
    LessonEvidence,
    LessonRegistry,
    LessonState,
    SkillRuleProvenance,
    load_registry,
    write_registry,
)
from mailman.knowledge.taxonomy import (
    EvidenceChannel,
    KnowledgeLayer,
    PatternCategory,
    Scope,
)


def sample_lesson(**overrides: object) -> LessonEntry:
    arguments: dict[str, object] = {
        "lesson_id": "LESSON-0001",
        "category": PatternCategory.TOOL_USAGE,
        "observation": (
            "The primary agent exited zero after every repository command was "
            "blocked."
        ),
        "hypothesis": (
            "Agents treat a zero exit code as proof that the task was done."
        ),
        "guidance": (
            "Judge the candidate repository state, not the agent exit code."
        ),
        "scope": Scope.UNIVERSAL,
    }
    arguments.update(overrides)
    return LessonEntry(**arguments)  # type: ignore[arg-type]


def observed_evidence(run_id: str) -> LessonEvidence:
    return LessonEvidence(
        run_id=run_id,
        channel=EvidenceChannel.AUTOMATED_VERIFICATION,
        observation_id="OBS-001",
    )


def promote_to_candidate(lesson: LessonEntry) -> None:
    lesson.transition(LessonState.HYPOTHESIS, "pattern seen twice")
    lesson.transition(LessonState.CANDIDATE_LESSON, "guidance written")


class LessonStateTests(unittest.TestCase):
    def test_path_to_the_skill_records_every_step(self) -> None:
        lesson = sample_lesson()
        promote_to_candidate(lesson)
        lesson.add_evidence(observed_evidence("run-1"))
        lesson.add_evidence(observed_evidence("run-2"))
        lesson.validation.append("Replay of run-1 and run-2 with the rule.")
        lesson.transition(LessonState.VALIDATED, "replay supported the rule")
        lesson.transition(LessonState.PROMOTED_TO_SKILL, "added to SKILL.md")

        self.assertEqual(lesson.state, LessonState.PROMOTED_TO_SKILL)
        self.assertEqual(len(lesson.history), 4)
        self.assertEqual(lesson.history[-1]["from"], "VALIDATED")

    def test_a_lesson_cannot_skip_straight_to_the_skill(self) -> None:
        lesson = sample_lesson()
        with self.assertRaisesRegex(ValueError, "invalid lesson transition"):
            lesson.transition(LessonState.PROMOTED_TO_SKILL, "it sounds right")

    def test_one_run_cannot_validate_a_lesson(self) -> None:
        lesson = sample_lesson()
        promote_to_candidate(lesson)
        lesson.add_evidence(observed_evidence("run-1"))
        lesson.validation.append("Replay of run-1.")
        with self.assertRaisesRegex(ValueError, "at least 2 distinct runs"):
            lesson.transition(LessonState.VALIDATED, "one vivid run")
        self.assertEqual(lesson.state, LessonState.CANDIDATE_LESSON)
        self.assertEqual(MINIMUM_VALIDATION_RUNS, 2)

    def test_agent_self_report_alone_cannot_validate_a_lesson(self) -> None:
        lesson = sample_lesson()
        promote_to_candidate(lesson)
        for run_id in ("run-1", "run-2"):
            lesson.add_evidence(
                LessonEvidence(
                    run_id=run_id, channel=EvidenceChannel.AGENT_RETROSPECTIVE
                )
            )
        lesson.validation.append("Both agents said so.")
        with self.assertRaisesRegex(ValueError, "machine or a human"):
            lesson.transition(LessonState.VALIDATED, "both agents agreed")

    def test_validation_needs_a_recorded_result(self) -> None:
        lesson = sample_lesson()
        promote_to_candidate(lesson)
        lesson.add_evidence(observed_evidence("run-1"))
        lesson.add_evidence(observed_evidence("run-2"))
        with self.assertRaisesRegex(ValueError, "at least one recorded result"):
            lesson.transition(LessonState.VALIDATED, "no replay was run")

    def test_a_hypothesis_and_guidance_are_required_before_their_states(
        self,
    ) -> None:
        lesson = sample_lesson(hypothesis="", guidance="")
        with self.assertRaisesRegex(ValueError, "hypothesis is required"):
            lesson.transition(LessonState.HYPOTHESIS, "no hypothesis yet")
        lesson.hypothesis = "Agents trust exit codes."
        lesson.transition(LessonState.HYPOTHESIS, "written")
        with self.assertRaisesRegex(ValueError, "candidate guidance is required"):
            lesson.transition(LessonState.CANDIDATE_LESSON, "no guidance yet")

    def test_rejection_is_terminal(self) -> None:
        lesson = sample_lesson()
        lesson.transition(LessonState.REJECTED, "contradicted by run-3")
        with self.assertRaisesRegex(ValueError, "invalid lesson transition"):
            lesson.transition(LessonState.HYPOTHESIS, "try again quietly")

    def test_a_promoted_rule_can_be_refined_by_a_later_run(self) -> None:
        lesson = sample_lesson()
        promote_to_candidate(lesson)
        lesson.add_evidence(observed_evidence("run-1"))
        lesson.add_evidence(observed_evidence("run-2"))
        lesson.validation.append("Replay comparison.")
        lesson.transition(LessonState.VALIDATED, "supported")
        lesson.transition(LessonState.PROMOTED_TO_SKILL, "promoted")
        lesson.refine(
            guidance="Judge the candidate state whenever the agent reports a block.",
            reason="run-4 narrowed the rule",
        )
        self.assertEqual(lesson.state, LessonState.REFINED)
        self.assertIn("reports a block", lesson.guidance)
        lesson.transition(LessonState.CANDIDATE_LESSON, "re-earning its state")


class KnowledgeLayerTests(unittest.TestCase):
    def test_conditional_knowledge_must_name_its_conditions(self) -> None:
        with self.assertRaisesRegex(ValueError, "name the conditions"):
            sample_lesson(layer=KnowledgeLayer.CONDITIONAL)

    def test_core_knowledge_cannot_carry_conditions(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot carry conditions"):
            sample_lesson(layer=KnowledgeLayer.CORE, conditions=["python"])

    def test_conditional_knowledge_round_trips_with_its_conditions(self) -> None:
        lesson = sample_lesson(
            layer=KnowledgeLayer.CONDITIONAL, conditions=["python", "monorepo"]
        )
        restored = LessonEntry.from_dict(lesson.to_dict())
        self.assertEqual(restored.conditions, ["python", "monorepo"])
        self.assertEqual(restored.layer, KnowledgeLayer.CONDITIONAL)


class RegistryTests(unittest.TestCase):
    def test_identifiers_are_sequential_and_unique(self) -> None:
        registry = LessonRegistry()
        self.assertEqual(registry.next_lesson_id(), "LESSON-0001")
        registry.add(sample_lesson())
        self.assertEqual(registry.next_lesson_id(), "LESSON-0002")
        with self.assertRaisesRegex(ValueError, "duplicate lesson ID"):
            registry.add(sample_lesson())

    def test_queries_by_state_and_category(self) -> None:
        registry = LessonRegistry()
        registry.add(sample_lesson())
        registry.add(
            sample_lesson(
                lesson_id="LESSON-0002", category=PatternCategory.TEST_DESIGN
            )
        )
        registry.get("LESSON-0002").transition(
            LessonState.HYPOTHESIS, "second sighting"
        )

        self.assertEqual(len(registry.by_state(LessonState.OBSERVATION)), 1)
        self.assertEqual(len(registry.by_category(PatternCategory.TEST_DESIGN)), 1)
        self.assertEqual(
            registry.state_counts(), {"OBSERVATION": 1, "HYPOTHESIS": 1}
        )
        with self.assertRaises(KeyError):
            registry.get("LESSON-9999")

    def test_registry_round_trips_through_a_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "knowledge" / "lessons.json"
            registry = LessonRegistry()
            lesson = registry.add(sample_lesson())
            lesson.add_evidence(observed_evidence("run-1"))
            written = write_registry(registry, path)

            self.assertEqual(written, path)
            restored = load_registry(path)
            self.assertEqual(restored.to_dict(), registry.to_dict())
            self.assertEqual(
                restored.get("LESSON-0001").evidence[0].run_id, "run-1"
            )
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_a_missing_registry_reads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "lessons.json"
            self.assertEqual(load_registry(path).lessons, [])

    def test_an_unsupported_schema_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "lessons.json"
            path.write_text(
                json.dumps({"schema_version": 99, "lessons": []}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "unsupported lesson registry"):
                load_registry(path)


class ProvenanceTests(unittest.TestCase):
    def validated_lesson(self) -> LessonEntry:
        lesson = sample_lesson()
        promote_to_candidate(lesson)
        lesson.add_evidence(observed_evidence("run-2"))
        lesson.add_evidence(observed_evidence("run-1"))
        lesson.validation.append("Replay of run-1 and run-2 with the rule.")
        lesson.transition(LessonState.VALIDATED, "replay supported the rule")
        return lesson

    def test_only_a_validated_lesson_can_be_promoted(self) -> None:
        with self.assertRaisesRegex(ValueError, "only a VALIDATED lesson"):
            SkillRuleProvenance.from_lesson(
                sample_lesson(),
                rule_id="RULE-0002",
                introduced="2026-09-02",
                skill_version="v0.2",
                expected_change="Agents stop trusting exit codes.",
            )

    def test_provenance_carries_runs_pattern_and_expected_change(self) -> None:
        provenance = SkillRuleProvenance.from_lesson(
            self.validated_lesson(),
            rule_id="RULE-0002",
            introduced="2026-09-02",
            skill_version="v0.2",
            expected_change=(
                "A reviewer names the candidate state instead of the exit code."
            ),
            follow_up="Not yet contradicted.",
        )
        block = provenance.to_yaml_block()

        self.assertIn("rule_id: RULE-0002", block)
        self.assertIn("failure_pattern: TOOL_USAGE", block)
        self.assertIn("skill_version: v0.2", block)
        self.assertIn("lesson_id: LESSON-0001", block)
        self.assertIn("  - run-1\n  - run-2", block)
        self.assertIn("validation:", block)
        self.assertTrue(block.startswith("```yaml\n"))
        self.assertTrue(block.endswith("```\n"))

    def test_a_rule_without_an_expected_behavior_change_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "behavior change"):
            SkillRuleProvenance(
                rule_id="RULE-0003",
                rule="Read the contributing guide.",
                reason="It seemed sensible.",
                introduced="2026-09-02",
                skill_version="v0.2",
                failure_pattern=PatternCategory.REPOSITORY_CONVENTIONS,
                expected_change="  ",
                motivating_runs=("run-1",),
            )

    def test_a_rule_without_motivating_runs_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "runs that motivated it"):
            SkillRuleProvenance(
                rule_id="RULE-0004",
                rule="Read the contributing guide.",
                reason="It seemed sensible.",
                introduced="2026-09-02",
                skill_version="v0.2",
                failure_pattern=PatternCategory.REPOSITORY_CONVENTIONS,
                expected_change="Agents read conventions first.",
            )

    def test_quotes_in_prose_do_not_break_the_yaml_block(self) -> None:
        provenance = SkillRuleProvenance(
            rule_id="RULE-0005",
            rule='Do not report a check as "passing" without a captured result.',
            reason="An agent said it was fine: it was not.",
            introduced="2026-09-02",
            skill_version="v0.2",
            failure_pattern=PatternCategory.VERIFICATION,
            expected_change="Reports cite a command record.",
            motivating_runs=("run-1",),
        )
        block = provenance.to_yaml_block()
        self.assertIn('\\"passing\\"', block)
        self.assertIn('reason: "An agent said it was fine: it was not."', block)


if __name__ == "__main__":
    unittest.main()
