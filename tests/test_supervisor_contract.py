from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clawpatch_supervise.checkpoint import CheckpointStore
from clawpatch_supervise.clawpatch_release import format_release_sweep
from clawpatch_supervise.errors import RuntimeBudgetExceeded, SafetyError
from clawpatch_supervise.git_ops import DirtySourcePolicy
from clawpatch_supervise.proof import write_completion_proof
from clawpatch_supervise.queue import QueueResult
from clawpatch_supervise.runtime_budget import RuntimeBudget


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "clawpatch_supervise"


class SupervisorContractTests(unittest.TestCase):
    def test_release_engine_is_split_into_bounded_components(self):
        for name in ("git_ops.py", "checkpoint.py", "queue.py", "validation.py", "proof.py"):
            line_count = len((PACKAGE_ROOT / name).read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(line_count, 3000, name)

        facade_lines = len(
            (PACKAGE_ROOT / "clawpatch_release.py").read_text(encoding="utf-8").splitlines()
        )
        self.assertLessEqual(facade_lines, 2500)

    def test_dirty_source_requires_explicit_adoption(self):
        with self.assertRaisesRegex(SafetyError, "--adopt-dirty"):
            DirtySourcePolicy().require_authorized(
                Path("/repo"), ["app.py"], context="Supervision"
            )

        DirtySourcePolicy(adopt_dirty=True).require_authorized(
            Path("/repo"), ["app.py"], context="Supervision"
        )

    def test_uncertain_queue_is_never_complete(self):
        result = QueueResult.from_report(
            {"finding_count": 4, "open_findings": 0, "uncertain_findings": 1}
        )
        self.assertFalse(result.complete)
        rendered = format_release_sweep(
            {
                "apply": True,
                "ok": True,
                "finding_count": 4,
                "open_findings": 0,
                "uncertain_findings": 1,
            }
        )
        self.assertIn("UNFINISHED", rendered)
        self.assertNotIn("COMPLETE", rendered)

    def test_completion_proof_refuses_uncertain_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(SafetyError, "uncertain=1"):
                write_completion_proof(
                    state_root=root,
                    repo=root / "repo",
                    branch="main",
                    git_head="abc123",
                    clawpatch_version="0.7.2",
                    completed_findings=[],
                    continuation_attempts=[],
                    false_positives=[],
                    review_generations=[],
                    final_closure={},
                    open_findings=0,
                    uncertain_findings=1,
                )

            self.assertFalse(CheckpointStore(root).proof_path.exists())

    def test_external_completion_proof_retains_uncertain_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            proof_path = write_completion_proof(
                state_root=root,
                repo=root / "repo",
                branch="main",
                git_head="abc123",
                clawpatch_version="0.7.2",
                completed_findings=[],
                continuation_attempts=[],
                false_positives=[],
                review_generations=[],
                final_closure={},
                open_findings=0,
                uncertain_findings=1,
                allow_uncertain=True,
            )

            proof = json.loads(proof_path.read_text(encoding="utf-8"))

        self.assertEqual(proof["status"], "COMPLETE")
        self.assertEqual(proof["open_findings"], 0)
        self.assertEqual(proof["uncertain_findings"], 1)

    def test_retry_budget_is_finite(self):
        budget = RuntimeBudget.start(minutes=1, max_retries=1)
        self.assertEqual(budget.consume_retry("first"), 1)
        with self.assertRaisesRegex(RuntimeBudgetExceeded, "retry budget"):
            budget.consume_retry("second")


if __name__ == "__main__":
    unittest.main()
