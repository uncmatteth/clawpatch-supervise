from __future__ import annotations

import unittest

from clawpatch_supervise.clawpatch_protocol import (
    ClawpatchFailureKind,
    RepairAction,
    classify_clawpatch_failure,
    decide_repair_transition,
    failure_from_legacy_outcome,
)


class ClawpatchProtocolTests(unittest.TestCase):
    def test_exit_codes_are_normalized_once_for_fix_and_revalidation(self):
        cases = (
            ("fix", 1, ClawpatchFailureKind.PROVIDER_FAILED),
            ("fix", 5, ClawpatchFailureKind.PROVIDER_QUOTA),
            ("fix", 6, ClawpatchFailureKind.VALIDATION_FAILED),
            ("fix", 124, ClawpatchFailureKind.TIMEOUT),
            ("revalidation", 4, ClawpatchFailureKind.PROVIDER_REFUSED),
            ("revalidation", 124, ClawpatchFailureKind.TIMEOUT),
            ("review", 2, ClawpatchFailureKind.INVALID_USAGE),
            ("review", 23, ClawpatchFailureKind.COMMAND_FAILED),
        )
        for phase, exit_code, expected in cases:
            with self.subTest(phase=phase, exit_code=exit_code):
                failure = classify_clawpatch_failure(phase, exit_code)
                self.assertEqual(failure.kind, expected)
                self.assertEqual(failure.phase, phase)
                self.assertEqual(failure.exit_code, exit_code)

    def test_any_progress_capable_external_failure_continues_only_with_new_source(self):
        for exit_code in (1, 4, 5, 6, 23, 124):
            with self.subTest(exit_code=exit_code):
                failure = classify_clawpatch_failure("fix", exit_code)
                self.assertEqual(
                    decide_repair_transition(
                        failure=failure,
                        has_source_progress=True,
                    ).action,
                    RepairAction.PRESERVE_AND_CONTINUE,
                )

    def test_no_progress_external_outcomes_split_transient_from_terminal(self):
        for exit_code in (1, 4, 5, 124):
            with self.subTest(exit_code=exit_code):
                failure = classify_clawpatch_failure("fix", exit_code)
                self.assertEqual(
                    decide_repair_transition(failure=failure).action,
                    RepairAction.STOP_TRANSIENT,
                )
        for exit_code in (2, 3, 6, 23):
            with self.subTest(exit_code=exit_code):
                failure = classify_clawpatch_failure("fix", exit_code)
                self.assertEqual(
                    decide_repair_transition(failure=failure).action,
                    RepairAction.STOP_TERMINAL,
                )

    def test_revalidation_outcomes_use_the_same_transition_policy(self):
        self.assertEqual(
            decide_repair_transition(revalidation_outcome="fixed").action,
            RepairAction.FINALIZE,
        )
        self.assertEqual(
            decide_repair_transition(revalidation_outcome="open").action,
            RepairAction.PRESERVE_AND_CONTINUE,
        )
        self.assertEqual(
            decide_repair_transition(revalidation_outcome="uncertain").action,
            RepairAction.STOP_TERMINAL,
        )
        self.assertEqual(
            decide_repair_transition(revalidation_outcome="false-positive").action,
            RepairAction.DISCARD_AND_CONTINUE,
        )

    def test_policy_rejects_ambiguous_or_unknown_events(self):
        with self.assertRaises(ValueError):
            decide_repair_transition()
        with self.assertRaises(ValueError):
            decide_repair_transition(
                failure=classify_clawpatch_failure("fix", 1),
                revalidation_outcome="open",
            )
        with self.assertRaises(ValueError):
            decide_repair_transition(revalidation_outcome="made-up")

    def test_legacy_checkpoint_outcomes_enter_the_same_typed_policy(self):
        failure = failure_from_legacy_outcome("revalidation-provider-failed")
        self.assertIsNotNone(failure)
        self.assertEqual(failure.kind, ClawpatchFailureKind.PROVIDER_REFUSED)
        self.assertIsNone(failure_from_legacy_outcome("made-up"))


if __name__ == "__main__":
    unittest.main()
