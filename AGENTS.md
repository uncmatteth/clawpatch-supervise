# ClawPatch Supervise

- Preserve the stdlib-only runtime dependency policy.
- Never use `shell=True`.
- ClawPatch owns finding selection and repair creation.
- Never skip, triage, or hide a finding. The external unattended command may commit an applied
  repair that ClawPatch records as `uncertain`, advance to the next open finding, and report the
  retained uncertain count honestly at completion; strict library mode must retain the old stop.
- Continue a same-finding repair only after proving a genuinely new exact source tree.
- Keep checkpoints outside the target worktree.
- Update tests and README with behavior changes.
- Before completion run `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
