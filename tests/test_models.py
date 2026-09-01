from __future__ import annotations

import unittest

from mailman.models import AgentConfig, RunRecord, RunStatus


def sample_run() -> RunRecord:
    return RunRecord(
        run_id="run-1",
        repository="https://github.com/example/project.git",
        issue="https://github.com/example/project/issues/1",
        base_commit="a" * 40,
        primary=AgentConfig("codex"),
        reviewer=AgentConfig("claude"),
    )


class RunStateTests(unittest.TestCase):
    def test_valid_path_stops_at_human_review(self) -> None:
        run = sample_run()
        run.transition(RunStatus.PRIMARY_RUNNING, "workspace ready")
        run.transition(RunStatus.REVIEW_PENDING, "candidate ready")
        run.transition(RunStatus.VERIFICATION_PENDING, "review approved")
        run.transition(RunStatus.READY_FOR_HUMAN_REVIEW, "checks passed")

        self.assertEqual(run.status, RunStatus.READY_FOR_HUMAN_REVIEW)
        self.assertEqual(len(run.history), 4)
        with self.assertRaisesRegex(ValueError, "invalid transition"):
            run.transition(RunStatus.PRIMARY_RUNNING, "try to bypass human")

    def test_cannot_skip_independent_review(self) -> None:
        run = sample_run()
        with self.assertRaisesRegex(ValueError, "invalid transition"):
            run.transition(RunStatus.READY_FOR_HUMAN_REVIEW, "skip")


if __name__ == "__main__":
    unittest.main()
