# ClawPatch Supervise

- Preserve the stdlib-only runtime dependency policy.
- Never use `shell=True`.
- ClawPatch owns finding selection and repair creation.
- Never skip, triage, hide, or advance an unresolved finding.
- Continue a same-finding repair only after proving a genuinely new exact source tree.
- Keep checkpoints outside the target worktree.
- Update tests and README with behavior changes.
- Before completion run `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
