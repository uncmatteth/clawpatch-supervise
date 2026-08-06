# ClawPatch Supervise

`clawpatch-supervise` is a standalone, stdlib-only Python supervisor for
[ClawPatch](https://www.npmjs.com/package/clawpatch) repair queues. It keeps one
finding active until ClawPatch reports that exact finding `fixed`, preserves
genuine partial source progress, and stops instead of silently skipping,
triaging, or advancing a failed finding.

## Why it exists

ClawPatch 0.7.2 exposes one-finding commands. Large repair queues need a durable
outer controller that can survive timeouts and provider failures without losing
source progress or turning an unchanged failure into an infinite retry loop.

This supervisor adds:

- complete map/review waves with progress checks;
- one-current-finding `next` to `show` to `fix` to `revalidate` transitions;
- exact-path temporary commits for genuine partial progress;
- exact source-tree cycle and no-progress detection;
- durable checkpoints outside the target repository;
- fixed-point review generations after repairs;
- a typed transient (`75`) versus terminal (`2`) service exit contract;
- Codex-only execution inherited from the target ClawPatch configuration, with
  no model fallback added.

## Requirements

- Python 3.11+
- Git
- ClawPatch 0.7.2+
- an authenticated provider configured in ClawPatch

## Install from source

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/clawpatch-supervise --help
```

## Run

```bash
clawpatch-supervise \
  --repo /absolute/path/to/repository \
  --branch current \
  --push none \
  --timeout-minutes 60 \
  --resume-stopped
```

For integrations that need the proof/checkpoint location without duplicating
platform path rules:

```bash
clawpatch-supervise --repo /absolute/path/to/repository --print-state-path
```

`--resume-stopped` does not mean "ignore failure." It accepts only an exact
durable checkpoint that still owns the same repository, branch, HEAD, finding,
source paths, and source fingerprint.

### Manageroo checkpoint compatibility

Version 0.1 reads and migrates checkpoints written by the original Manageroo
supervisor and accepts its temporary iteration commit identity. Those legacy
names remain only in the compatibility schema; Manageroo is not a runtime
dependency.

## systemd transient service

Only typed transient failures should restart:

```bash
systemd-run --user --unit clawpatch-supervise \
  --property=Restart=no \
  --property=RestartForceExitStatus=75 \
  --property=RestartSec=30s \
  clawpatch-supervise --repo "$PWD" --branch current --push none \
  --timeout-minutes 60 --resume-stopped
```

Exit `0` means complete, `75` means a classified transient provider, refusal,
quota, or timeout stop, and `2` means terminal or safety stop.

## Safety boundary

The supervisor never invents a repair, parses a report into its own queue,
switches providers, stashes source, marks a finding resolved, or pushes a
temporary iteration commit. ClawPatch owns finding content and repair creation;
the supervisor owns sequencing, checkpoints, validation boundaries, commits,
and completion proof.

## Test

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

No GitHub Actions are included. Release proof is local and explicit.

## Origin

Extracted from Uncle Matt's Project Manageroo after operating long ClawPatch
repair queues exposed the need for a separately versioned supervisor boundary.

## License

MIT © 2026 Uncle Matt
