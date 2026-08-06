---
name: clawpatch-supervise
description: Install, run, recover, or explain the standalone ClawPatch queue supervisor. Use when someone asks to run a whole ClawPatch repair queue, resume a stopped finding, keep ClawPatch running outside the repo it repairs, or prove the queue actually finished.
---

# ClawPatch Supervise

Use the standalone supervisor as the outer runtime for a ClawPatch repair queue.

## Contract

- ClawPatch owns review, finding selection, repair, revalidation, and finding status.
- The supervisor owns ordering, watchdogs, exact checkpoints, commits, optional pushes, and completion proof.
- Run the supervisor outside the target repository's implementation environment, even when the target is Manageroo itself.
- Never skip, triage, hide, or manually repair an unresolved ClawPatch finding.
- Never discard dirty source unless an exact supervisor checkpoint proves ownership.
- Do not call an active queue complete. Require the final `COMPLETE` line and proof file.

## Workflow

1. Read the target repository's instructions and current Git state.
2. Verify `git`, Python 3.11+, `clawpatch --version`, and `clawpatch-supervise --version`.
3. Check for another supervisor or ClawPatch process targeting the same repository.
4. Choose one start mode:
   - `--fresh` for a new map and complete review generation.
   - `--resume-stopped` only for the exact stopped checkpoint already on disk.
5. Run one repository at a time with an explicit absolute path, branch policy, push policy, and watchdog.
6. If it stops, preserve the printed finding, paths, checkpoint, and source exactly. Diagnose the ownership or command failure before relaunching.
7. On completion, verify the proof file, clean Git state, local HEAD, and remote SHA when pushes were enabled.

Linux or macOS:

```bash
clawpatch-supervise --repo /absolute/path/to/repo --branch current \
  --push each --timeout-minutes 15 --fresh
```

Windows PowerShell:

```powershell
clawpatch-supervise --repo "C:\absolute\path\to\repo" --branch current `
  --push each --timeout-minutes 15 --fresh
```

Resume only an exact stopped attempt:

```text
clawpatch-supervise --repo <absolute-path> --branch current --push each --timeout-minutes 15 --resume-stopped
```

Find the external checkpoint and proof directory:

```text
clawpatch-supervise --repo <absolute-path> --print-state-path
```

## Output

Return:

- repository and branch;
- supervisor and ClawPatch versions;
- fresh or resumed mode;
- current finding and phase, or final `COMPLETE` result;
- exact stopped paths and exit class when incomplete;
- proof path, clean/dirty Git state, local SHA, and remote SHA when complete.

## Anti-patterns

- Do not replace the command-owned queue with a report-derived loop.
- Do not run `clawpatch next` to get around a stopped finding.
- Do not rerun the same fix against an unchanged source tree.
- Do not commit unrelated or pre-existing changes.
- Do not run ClawPatch against Git submodule contents owned by third parties; fresh supervisor runs exclude Gitlinks automatically.
- Do not claim Windows or Linux support from source inspection alone; use the repository's cross-platform test results.

## Proof

Project and install instructions: https://github.com/uncmatteth/clawpatch-supervise

ClawHub page: https://clawhub.ai/uncmatteth/skills/clawpatch-supervise

The run is complete only when the supervisor exits `0`, writes a `status: COMPLETE` proof, reports zero open or uncertain findings after a fresh review generation, and leaves the repository in the expected Git state.
