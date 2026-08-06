from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clawpatch_supervise.clawpatch_release import (
    _UnresolvedFinding,
    _final_closure,
    _load_release_progress,
    _prepare_fresh_release,
    _process_finding_until_fixed,
    _restore_committed_clawpatch_state,
    _resolve_uncertain_findings,
    _write_release_progress,
)


class ClawpatchPartialProgressTests(unittest.TestCase):
    @staticmethod
    def init_repo(repo: Path) -> tuple[str, str]:
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=repo,
            check=True,
        )
        (repo / ".gitignore").write_text(".clawpatch/\n", encoding="utf-8")
        (repo / "app.py").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore", "app.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
        ).strip()
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
        return branch, head

    def test_final_state_cleanup_restores_committed_config_and_removes_runtime_state(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            _branch, _head = self.init_repo(repo)
            state = repo / ".clawpatch"
            state.mkdir()
            (state / "config.json").write_text('{"committed":true}\n', encoding="utf-8")
            (state / "project.json").write_text('{"name":"original"}\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", ".clawpatch/config.json", ".clawpatch/project.json"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "commit", "-q", "-m", "track config"], cwd=repo, check=True)

            (state / "config.json").write_text('{"committed":false}\n', encoding="utf-8")
            (state / "runs").mkdir()
            (state / "runs" / "generated.json").write_text("{}\n", encoding="utf-8")
            source_before = (repo / "app.py").read_bytes()

            _restore_committed_clawpatch_state(repo)

            self.assertEqual(
                (state / "config.json").read_text(encoding="utf-8"),
                '{"committed":true}\n',
            )
            self.assertEqual(
                (state / "project.json").read_text(encoding="utf-8"),
                '{"name":"original"}\n',
            )
            self.assertFalse((state / "runs").exists())
            self.assertEqual((repo / "app.py").read_bytes(), source_before)
            self.assertEqual(
                subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True),
                "",
            )

    @patch("clawpatch_supervise.clawpatch_release._push_and_verify")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_overlapping_finding_fixed_by_prior_commit_needs_no_second_commit(
        self,
        execute_fix,
        _processes,
        push_and_verify,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            branch, _base_head = self.init_repo(repo)
            (repo / "app.py").write_text(
                "already repaired by prior finding\n", encoding="utf-8"
            )
            subprocess.run(["git", "add", "--", "app.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "prior finding"], cwd=repo, check=True
            )
            starting_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            state_root = root / "state"
            progress_events = []
            execute_fix.return_value = (
                {
                    "finding_id": "fnd_overlap",
                    "files_changed": [],
                    "revalidation": {"finding": "fnd_overlap", "outcome": "fixed"},
                    "commit": "",
                },
                False,
            )

            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_overlap",
                inspected={"finding": {"id": "fnd_overlap", "status": "open"}},
                env={},
                push_mode="each",
                branch=branch,
                pushed=True,
                state_root=state_root,
                progress=progress_events.append,
            )

            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo, text=True
                ).strip(),
                starting_head,
            )
            self.assertEqual(record["files_changed"], [])
            self.assertEqual(record["commit"], "")
            self.assertTrue(pushed)
            self.assertEqual(continuations, 0)
            self.assertEqual(
                subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True),
                "",
            )
            self.assertIsNone(_load_release_progress(repo, state_root=state_root))
            self.assertNotIn("commit", [event["phase"] for event in progress_events])
            self.assertNotIn("push", [event["phase"] for event in progress_events])
            push_and_verify.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._push_and_verify")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_partial_repair_is_locally_preserved_then_finished_as_one_final_commit(
        self,
        execute_fix,
        _processes,
        push_and_verify,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            branch, original_head = self.init_repo(repo)
            state_root = root / "state"
            calls = 0
            progress_events = []

            def fix_side_effect(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                source = repo / "app.py"
                if calls == 1:
                    source.write_text("before\npartial\n", encoding="utf-8")
                    raise _UnresolvedFinding(
                        "validation failed after applying fix",
                        finding_id="fnd_one",
                        outcome="fix-validation-failed",
                    )
                self.assertEqual(
                    subprocess.check_output(
                        ["git", "status", "--porcelain"], cwd=repo, text=True
                    ),
                    "",
                )
                self.assertEqual(source.read_text(encoding="utf-8"), "before\npartial\n")
                source.write_text("before\npartial\nfinished\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["app.py"],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = fix_side_effect
            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={"finding": {"id": "fnd_one", "status": "open"}},
                env={},
                push_mode="each",
                branch=branch,
                pushed=False,
                state_root=state_root,
                progress=progress_events.append,
            )

            final_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            commit_count = subprocess.check_output(
                ["git", "rev-list", "--count", f"{original_head}..{final_head}"],
                cwd=repo,
                text=True,
            ).strip()
            committed_paths = subprocess.check_output(
                ["git", "diff", "--name-only", original_head, final_head],
                cwd=repo,
                text=True,
            ).splitlines()
            status = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=repo, text=True
            )

            self.assertEqual(calls, 2)
            self.assertEqual(commit_count, "1")
            self.assertEqual(committed_paths, ["app.py"])
            self.assertEqual(
                (repo / "app.py").read_text(encoding="utf-8"),
                "before\npartial\nfinished\n",
            )
            self.assertEqual(status, "")
            self.assertEqual(record["commit"], final_head)
            self.assertEqual(continuations, 1)
            self.assertTrue(pushed)
            self.assertIsNone(_load_release_progress(repo, state_root=state_root))
            push_and_verify.assert_called_once_with(repo, branch, first=True)
            self.assertEqual(
                [event["attempt"] for event in progress_events if event["phase"] == "fix"],
                [1, 2],
            )
            self.assertEqual(
                [event["phase"] for event in progress_events if event["phase"] in {"commit", "push"}],
                ["commit", "push"],
            )

    @patch("clawpatch_supervise.clawpatch_release._push_and_verify")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_no_progress_unwinds_temporary_commit_and_leaves_partial_source_visible(
        self,
        execute_fix,
        _processes,
        push_and_verify,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            branch, original_head = self.init_repo(repo)
            state_root = root / "state"
            calls = 0

            def fix_side_effect(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    (repo / "app.py").write_text(
                        "before\nvaluable partial\n", encoding="utf-8"
                    )
                raise _UnresolvedFinding(
                    "validation failed after applying fix: database assertion failed",
                    finding_id="fnd_one",
                    outcome="fix-validation-failed",
                )

            execute_fix.side_effect = fix_side_effect
            with self.assertRaisesRegex(Exception, "no source changes") as caught:
                _process_finding_until_fixed(
                    repo,
                    "fnd_one",
                    inspected={"finding": {"id": "fnd_one", "status": "open"}},
                    env={},
                    push_mode="each",
                    branch=branch,
                    pushed=False,
                    state_root=state_root,
                )

            self.assertIn(
                "Original Clawpatch failure: validation failed after applying fix: "
                "database assertion failed",
                str(caught.exception),
            )

            current_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            status = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=repo, text=True
            )
            checkpoint = _load_release_progress(repo, state_root=state_root)

            self.assertEqual(calls, 2)
            self.assertEqual(current_head, original_head)
            self.assertEqual(
                (repo / "app.py").read_text(encoding="utf-8"),
                "before\nvaluable partial\n",
            )
            self.assertIn("app.py", status)
            self.assertEqual(checkpoint["phase"], "stopped")
            self.assertEqual(checkpoint["owned_paths"], ["app.py"])
            self.assertTrue(checkpoint["temporary_commit"])
            push_and_verify.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_interrupted_temporary_commit_is_proven_recovered_and_freshly_initialized(
        self,
        json_clawpatch,
        _processes,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            branch, original_head = self.init_repo(repo)
            (repo / "unrelated.txt").write_text("preserve me\n", encoding="utf-8")
            config = repo / ".clawpatch" / "config.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"provider":"codex"}\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "-f", "unrelated.txt", ".clawpatch/config.json"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "commit", "-q", "-m", "config"], cwd=repo, check=True)
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            (repo / "app.py").write_text("before\npartial\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "app.py"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "commit.gpgSign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "-q",
                    "-m",
                    "manageroo clawpatch iteration: fnd_one",
                ],
                cwd=repo,
                check=True,
            )
            temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            state_root = root / "state"
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=original_head,
                phase="iteration",
                owned_paths=["app.py"],
                temporary_commit=temporary_commit,
                source_states=[
                    subprocess.check_output(
                        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True
                    ).strip()
                ],
                state_root=state_root,
            )

            def initialize(*_args, **_kwargs):
                project = repo / ".clawpatch" / "project.json"
                project.parent.mkdir(parents=True, exist_ok=True)
                project.write_text("{}\n", encoding="utf-8")
                return {"created": True}

            json_clawpatch.side_effect = initialize
            _prepare_fresh_release(
                repo,
                env={},
                state_root=state_root,
            )

            current_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            self.assertEqual(current_head, original_head)
            self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "before\n")
            self.assertEqual(
                (repo / "unrelated.txt").read_text(encoding="utf-8"), "preserve me\n"
            )
            self.assertEqual(config.read_text(encoding="utf-8"), '{"provider":"codex"}\n')
            self.assertTrue((repo / ".clawpatch/project.json").is_file())
            self.assertIsNone(_load_release_progress(repo, state_root=state_root))
            json_clawpatch.assert_called_once()

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_fresh_retires_dangling_temporary_commit_after_branch_advances_cleanly(
        self,
        json_clawpatch,
        _processes,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            branch, original_head = self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\npartial repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "app.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "manageroo clawpatch iteration: fnd_one"],
                cwd=repo,
                check=True,
            )
            temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            state_root = root / "state"
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=original_head,
                phase="stopped",
                owned_paths=["app.py"],
                temporary_commit=temporary_commit,
                source_states=[
                    subprocess.check_output(
                        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True
                    ).strip()
                ],
                state_root=state_root,
            )

            subprocess.run(["git", "reset", "--hard", original_head], cwd=repo, check=True)
            source.write_text("before\nnew committed user work\n", encoding="utf-8")
            user_file = repo / "user.txt"
            user_file.write_text("preserve me\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", "app.py", "user.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "new user work"], cwd=repo, check=True)
            current_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()

            def initialize(*_args, **_kwargs):
                project = repo / ".clawpatch" / "project.json"
                project.parent.mkdir(parents=True, exist_ok=True)
                project.write_text("{}\n", encoding="utf-8")
                return {"created": True}

            json_clawpatch.side_effect = initialize
            _prepare_fresh_release(repo, env={}, state_root=state_root)

            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo, text=True
                ).strip(),
                current_head,
            )
            self.assertEqual(source.read_text(encoding="utf-8"), "before\nnew committed user work\n")
            self.assertEqual(user_file.read_text(encoding="utf-8"), "preserve me\n")
            self.assertEqual(
                subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True),
                "",
            )
            self.assertIsNone(_load_release_progress(repo, state_root=state_root))
            json_clawpatch.assert_called_once()

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_fresh_refuses_unrelated_dirty_source_and_changes_nothing(
        self,
        json_clawpatch,
        _processes,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            branch, original_head = self.init_repo(repo)
            state_root = root / "state"
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=original_head,
                phase="stopped",
                owned_paths=["app.py"],
                state_root=state_root,
            )
            (repo / "app.py").write_text("before\nowned partial\n", encoding="utf-8")
            (repo / "user.txt").write_text("unrelated work\n", encoding="utf-8")
            before_status = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=repo, text=True
            )

            with self.assertRaisesRegex(
                Exception,
                "A fresh Clawpatch run refuses unrelated source changes: app.py, user.txt",
            ):
                _prepare_fresh_release(repo, env={}, state_root=state_root)

            after_status = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=repo, text=True
            )
            self.assertEqual(after_status, before_status)
            self.assertEqual(
                (repo / "app.py").read_text(encoding="utf-8"),
                "before\nowned partial\n",
            )
            self.assertEqual(
                (repo / "user.txt").read_text(encoding="utf-8"), "unrelated work\n"
            )
            self.assertIsNotNone(_load_release_progress(repo, state_root=state_root))
            json_clawpatch.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    def test_existing_uncertain_finding_is_revalidated_before_final_closure(
        self,
        next_finding,
        show_finding,
        revalidate,
        _gates,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            queue = {
                "finding": {"id": "fnd_uncertain", "status": "uncertain"},
                "next": "clawpatch show --finding fnd_uncertain",
            }
            next_finding.side_effect = [
                ("fnd_uncertain", queue),
                (None, {"finding": None, "status": "uncertain"}),
            ]
            show_finding.return_value = {
                "finding": {"id": "fnd_uncertain", "status": "uncertain"},
                "validation": ["npm test"],
                "patchAttempts": [],
            }
            revalidate.return_value = {
                "finding": "fnd_uncertain",
                "outcome": "fixed",
                "managerooSandboxEscalated": True,
            }

            records, reopened = _resolve_uncertain_findings(
                repo,
                env={},
                uncertain_total=1,
                require_project_gates=False,
            )

            self.assertEqual(reopened, [])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["finding_id"], "fnd_uncertain")
            self.assertEqual(records[0]["commit"], "")
            self.assertEqual(next_finding.call_count, 2)
            revalidate.assert_called_once_with(
                repo,
                "fnd_uncertain",
                env={},
                expected_paths=[],
                progress=None,
                current=1,
                total=1,
            )

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._resolve_uncertain_findings")
    @patch("clawpatch_supervise.clawpatch_release._review_completion", return_value={"done": True})
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_final_closure_recovers_uncertain_finding_then_requires_fresh_review(
        self,
        json_clawpatch,
        _review,
        resolve_uncertain,
        next_finding,
        _gates,
        _processes,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            branch, _head = self.init_repo(repo)
            (repo / ".clawpatch" / "runs").mkdir(parents=True)
            (repo / ".clawpatch" / "runs" / "generated.json").write_text(
                "{}\n", encoding="utf-8"
            )
            recovered = {
                "finding_id": "fnd_uncertain",
                "revalidation": {"outcome": "fixed"},
                "commit": "",
                "recovered_uncertain": True,
            }
            resolve_uncertain.return_value = ([recovered], [])
            next_finding.return_value = (None, {"finding": None})
            json_clawpatch.side_effect = [
                {"revalidated": 0},
                {"total": 0, "items": []},
                {"total": 1, "items": [{"id": "fnd_uncertain"}]},
                {"revalidated": 0},
                {"total": 0, "items": []},
                {"total": 0, "items": []},
                {"openFindings": 0, "activeLocks": 0, "lockFiles": 0},
            ]

            closure = _final_closure(
                repo,
                env={},
                state_root=root / "state",
                push_mode="none",
                branch=branch,
                pushed=False,
                publish_clawpatch_state=False,
                review_limit=1,
                current=9,
                total=9,
                require_project_gates=False,
            )

            self.assertEqual(closure["recovered_findings"], [recovered])
            self.assertTrue(closure["needs_fresh_review"])
            self.assertTrue((repo / ".clawpatch").exists())
            resolve_uncertain.assert_called_once_with(
                repo,
                env={},
                uncertain_total=1,
                require_project_gates=False,
                progress=None,
                current_offset=9,
            )

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._process_finding_until_fixed")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._resolve_uncertain_findings")
    @patch("clawpatch_supervise.clawpatch_release._review_completion", return_value={"done": True})
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_uncertain_revalidation_that_reopens_uses_normal_same_finding_repair_loop(
        self,
        json_clawpatch,
        _review,
        resolve_uncertain,
        next_finding,
        show_finding,
        process_finding,
        _gates,
        _processes,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            branch, _head = self.init_repo(repo)
            resolve_uncertain.return_value = ([], ["fnd_uncertain"])
            queue = {
                "finding": {"id": "fnd_uncertain", "status": "open"},
                "next": "clawpatch show --finding fnd_uncertain",
            }
            next_finding.side_effect = [
                ("fnd_uncertain", queue),
                (None, {"finding": None}),
            ]
            inspected = {
                "finding": {"id": "fnd_uncertain", "status": "open"},
                "validation": [],
                "patchAttempts": [],
            }
            show_finding.return_value = inspected
            repaired = {
                "finding_id": "fnd_uncertain",
                "revalidation": {"outcome": "fixed"},
                "commit": "abc123",
            }
            process_finding.return_value = (repaired, True, 1)
            json_clawpatch.side_effect = [
                {"revalidated": 0},
                {"total": 0, "items": []},
                {"total": 1, "items": [{"id": "fnd_uncertain"}]},
                {"revalidated": 0},
                {"total": 0, "items": []},
                {"total": 0, "items": []},
                {"openFindings": 0, "activeLocks": 0, "lockFiles": 0},
            ]

            closure = _final_closure(
                repo,
                env={},
                state_root=root / "state",
                push_mode="each",
                branch=branch,
                pushed=False,
                publish_clawpatch_state=False,
                review_limit=1,
                current=9,
                total=9,
                require_project_gates=False,
            )

            self.assertEqual(len(closure["recovered_findings"]), 1)
            self.assertTrue(closure["recovered_findings"][0]["recovered_uncertain"])
            self.assertEqual(
                closure["recovered_continuations"],
                [
                    {
                        "finding_id": "fnd_uncertain",
                        "iteration": 1,
                        "temporary_local_commit": True,
                    }
                ],
            )
            process_finding.assert_called_once()
            self.assertEqual(process_finding.call_args.args[:2], (repo, "fnd_uncertain"))


if __name__ == "__main__":
    unittest.main()
