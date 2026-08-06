# ClawPatch Supervise

> The outside-the-repo supervisor that keeps a ClawPatch repair queue honest, resumable, and moving without skipping the finding that caused trouble.

[![Cross-platform tests](https://github.com/uncmatteth/clawpatch-supervise/actions/workflows/test.yml/badge.svg)](https://github.com/uncmatteth/clawpatch-supervise/actions/workflows/test.yml)
[![Latest release](https://img.shields.io/github/v/release/uncmatteth/clawpatch-supervise)](https://github.com/uncmatteth/clawpatch-supervise/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

`clawpatch-supervise` runs the long job around [ClawPatch](https://www.npmjs.com/package/clawpatch). ClawPatch still reviews the code, selects the current finding, writes the repair, and revalidates it. This supervisor remembers exactly where the queue was, protects real source progress, prevents unchanged retry loops, commits only verified repair paths, optionally pushes them, and refuses to call the job complete until a fresh review generation proves there is nothing left.

It is a standalone Python package, a command-line program, and a public [ClawHub skill](https://clawhub.ai/uncmatteth/skills/clawpatch-supervise). It runs on Linux and Windows and has no Python runtime dependencies outside the standard library.

## Quick start

You need Python 3.11 or newer, Git, Node/npm, and a provider already authenticated for ClawPatch. If the `clawpatch` command is missing, either installer below installs `clawpatch@latest` globally with npm and verifies the command. An existing ClawPatch installation is left unchanged; ClawPatch 0.7.2 or newer is required.

### Linux

```bash
git clone https://github.com/uncmatteth/clawpatch-supervise.git
cd clawpatch-supervise
./scripts/install.sh
clawpatch-supervise --version
```

The installer creates an isolated virtual environment under `~/.local/share/clawpatch-supervise` and puts the command in `~/.local/bin`. If ClawHub is missing, it also installs the latest ClawHub CLI into that isolated root and exposes `clawhub` from the same bin directory. An existing ClawHub installation is preserved. If that directory is not already on `PATH`, reopen your terminal or run:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### Windows PowerShell

```powershell
git clone https://github.com/uncmatteth/clawpatch-supervise.git
Set-Location clawpatch-supervise
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1 -AddToPath
clawpatch-supervise --version
```

The Windows installer creates an isolated environment under `%LOCALAPPDATA%\ClawPatchSupervise`. If ClawHub is missing, it also installs the latest ClawHub CLI into that isolated root and creates `clawhub.cmd`; an existing installation is preserved. Open a new PowerShell window after `-AddToPath`, or use the printed `.cmd` paths immediately.

### Run a queue

Linux:

```bash
clawpatch-supervise \
  --repo /absolute/path/to/your/repository \
  --branch current \
  --push each \
  --timeout-minutes 15 \
  --fresh
```

Windows PowerShell:

```powershell
clawpatch-supervise `
  --repo "C:\absolute\path\to\your\repository" `
  --branch current `
  --push each `
  --timeout-minutes 15 `
  --fresh
```

Use `--push none` if you want verified local commits without publishing them. Use `--resume-stopped` instead of `--fresh` only when the same repository already has an exact stopped supervisor checkpoint.

## Why I made this

I built this because a ClawPatch queue is not the same thing as one ClawPatch command.

On a real repository, a repair can take several attempts. A provider can time out after changing source. Validation can fail after the first useful edit. Revalidation can reopen the same finding. A process can die with a good partial repair still sitting in the worktree. If you blindly run the same command again, you can loop forever. If you call `clawpatch next`, you can abandon the exact finding that still needs to be resolved. If you let a chat session remember the queue, context compaction or a closed terminal can erase the only copy of what happened.

I ran into those failures while using ClawPatch on Manageroo and other actual projects. I needed something outside the target repository that could answer five boring but critical questions every time:

1. Which exact finding owns these changes?
2. Is this a genuinely new source tree or the same failed attempt again?
3. Can this repair be committed without pulling unrelated work into it?
4. If the process stops, can the next process prove exactly what it is resuming?
5. Did the entire queue really finish, including a fresh final review, or did the terminal merely stop printing?

That is what this program does. It is deliberately more stubborn than a shell loop and less creative than an AI agent. It follows the command-owned lifecycle, records durable evidence, and stops only when continuing would mean guessing, losing work, or lying about completion.

## What it actually supervises

```text
target repository
      │
      ▼
ClawPatch map and complete review
      │
      ▼
next → show → fix → revalidate
             │          │
             │          ├─ fixed ────────────────┐
             │          ├─ open + new tree ──┐   │
             │          ├─ false-positive ───┼───┤
             │          └─ failure/no proof ─┘   │
             │                                   │
             └──── exact checkpoint/commit ◀─────┘
                              │
                              ▼
fresh map and complete review generation
                              │
                              ▼
COMPLETE only at zero remaining work
```

The supervisor provides:

- complete map/review waves with a decreasing-pending proof;
- one-current-finding `next → show → fix → revalidate` ordering;
- exact-path temporary commits for genuine partial progress;
- same-finding continuation only when ClawPatch produced a new source tree;
- durable checkpoints outside the repository being repaired;
- a child-process watchdog that terminates the entire timed-out process group;
- optional verified `each` or `final` pushes;
- fresh fixed-point review generations after repairs;
- exact `false-positive` cleanup of only supervisor-owned repair paths;
- automatic exclusion of Git submodules from fresh ClawPatch mapping;
- recursive fingerprints for dirty nested Git repositories, so two different dependency edits cannot masquerade as the same checkpoint;
- a final proof file bound to the repository, branch, Git SHA, review generation, findings, and closure checks.

## What happens after each outcome

| ClawPatch result | Supervisor action |
|---|---|
| `fixed` with a verified repair | Create one exact-path repair commit, push if requested, continue. |
| `open` with a genuinely new source tree | Preserve the iteration locally and re-enter the same finding. |
| Validation/provider failure after new source progress | Preserve the exact progress and continue the same finding. |
| `false-positive` | Restore only the exact supervisor-owned repair paths to the finding's starting tree, retire the checkpoint, continue. |
| Same finding returns the same tree or no source changes | Stop the loop; another identical fix call cannot make progress. |
| Transient provider, refusal, quota, or timeout with no source progress | Exit `75` so a service may restart later. |
| Ownership, branch, checkpoint, or Git provenance mismatch | Exit `2` and leave the source for inspection. |
| Fresh final review proves zero remaining work | Write `status: COMPLETE` proof and exit `0`. |

## The Git submodule failure that forced version 0.1.4

Git stores a submodule in its parent repository as one pointer. A file can change two repositories deep while the top-level pointer stays exactly the same. Ordinary `git add lib/dependency` cannot put that inner edit into the parent commit.

Earlier releases correctly refused to create an incomplete commit, but they discovered the ownership problem only after ClawPatch had already reviewed and repaired third-party dependency source. Version 0.1.4 fixes both sides of that problem:

- fresh runs automatically add every top-level Gitlink and its descendants to ClawPatch's exclude list;
- checkpoint fingerprints recursively hash actual tracked and untracked content inside any dirty nested repository.

The result is simple: ClawPatch reviews the code the target repository actually owns. The supervisor does not publish detached commits into someone else's dependency or pretend an unstaged nested repair is complete.

## The interrupted-checkpoint failure fixed in version 0.1.5

Older supervisors could leave a verified temporary repair commit after already returning HEAD and the worktree to the finding's original clean state. Some legacy checkpoints recorded an empty owned-path list for that now-dangling commit. A later `--fresh` run would stop with `Interrupted Clawpatch temporary commit paths do not match its checkpoint` even though there was no source left to protect.

Version 0.1.5 recognizes only the provably safe form of that state: the commit must still be a valid supervisor iteration for the same finding and branch, HEAD must equal its recorded parent, and the worktree must contain zero source changes. The fresh run then retires the stale checkpoint and remaps normally. Any dirty, moved, or ambiguous source still stops.

## The evidence-informed recovery added in version 0.1.6

A source-clean stopped checkpoint can be followed by a later manual or restarted `clawpatch fix` attempt at the same HEAD. Earlier supervisors kept reading the checkpoint's old empty owned-path list even when ClawPatch had since recorded a real applied repair. That produced `Stopped Clawpatch progress has no source changes and no matching planned attempt at the current HEAD` while the correct repair was visibly present.

Version 0.1.6 recognizes that later repair only when the finding ID, applied status, base SHA, and complete current source-path set all match. It fingerprints those exact paths into the durable checkpoint, revalidates, commits, pushes when authorized, and continues the queue. The terminal names this transition `RESUME APPLIED REPAIR` and lists every owned path.

The same release also uses an open no-source revalidation as new evidence for up to two additional same-finding fix attempts. This handles the real case where the first agent incorrectly claims no edit is needed, revalidation disproves that claim, and the next fix succeeds. It never advances to another finding, and source-producing iterations remain governed by exact tree and fingerprint checks.

## How checkpoints work

The checkpoint is not a vague “last item” marker. It binds:

- the absolute repository identity;
- branch and starting HEAD;
- exact finding ID;
- exact owned source paths;
- recursive source fingerprint;
- temporary iteration commit, when present;
- every previously seen source-tree state;
- last typed repair action.

Ask the program for the platform-correct state directory:

```bash
clawpatch-supervise --repo /absolute/path/to/repo --print-state-path
```

Default state homes are:

- Linux: `${XDG_STATE_HOME:-~/.local/state}/clawpatch-supervise`
- Windows: `%LOCALAPPDATA%\ClawPatchSupervise\state`

`--resume-stopped` accepts only an exact matching checkpoint. It does not mean “ignore the last error.”

When a checkpoint moves from an older Manageroo installation, the supervisor upgrades its
source fingerprint only after the legacy algorithm still proves the exact current files. A
mismatch remains a safety stop; migrated state never grants ownership to changed source.

## Manageroo and the standalone supervisor

This is part of the [Uncle Matt's Project Manageroo](https://github.com/uncmatteth/Uncle-Matts-Project-Manageroo) system. It was separated into its own repository and process so it can run outside the repository it repairs—including when the repository being repaired is Manageroo itself.

```text
Manageroo
  ├─ installation, integration, policy, project gates, and final product proof
  └─ calls/updates
       └─ clawpatch-supervise outside the target worktree
            └─ calls ClawPatch inside the target repository
```

Separate does not mean removed. Manageroo is the front door and integration owner. `clawpatch-supervise` is the independently versioned queue runtime. ClawPatch remains the repair engine.

## Keep it running on Linux

A systemd user service gives you a background process, logs, and typed restart behavior:

```bash
systemd-run --user \
  --unit=clawpatch-supervise \
  --description="ClawPatch repair queue" \
  --property=WorkingDirectory="$PWD" \
  --property=Restart=on-failure \
  --property=RestartPreventExitStatus=2 \
  --property=RestartSec=30s \
  clawpatch-supervise --repo "$PWD" --branch current --push each \
    --timeout-minutes 15 --resume-stopped
```

Watch it:

```bash
journalctl --user -fu clawpatch-supervise.service
```

Closing the log viewer does not stop the service. Check the process itself with:

```bash
systemctl --user status clawpatch-supervise.service
```

## Keep it running on Windows

Run it in the foreground when you want the clearest output. To launch it without keeping a PowerShell window open:

```powershell
$Supervisor = "$env:LOCALAPPDATA\ClawPatchSupervise\venv\Scripts\clawpatch-supervise.exe"
$Repo = "C:\absolute\path\to\your\repository"
$Logs = "$env:LOCALAPPDATA\ClawPatchSupervise\logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null

Start-Process -FilePath $Supervisor `
  -ArgumentList @("--repo", $Repo, "--branch", "current", "--push", "each", "--timeout-minutes", "15", "--resume-stopped") `
  -RedirectStandardOutput "$Logs\queue.out.log" `
  -RedirectStandardError "$Logs\queue.err.log" `
  -WindowStyle Hidden

Get-Content "$Logs\queue.out.log" -Wait
```

For unattended restart after sign-in or reboot, create a Windows Task Scheduler task that invokes the same installed `.exe` and arguments. Exit `75` is retryable; exit `2` is a terminal/safety stop that needs inspection.

## Safety boundary

The supervisor never:

- invents or hand-writes a repair;
- parses a report into a shadow queue;
- calls `next` to skip a stopped finding;
- triages, hides, or marks a finding resolved;
- switches providers or adds a fallback model;
- stashes source changes;
- commits unrelated or pre-existing files;
- publishes a temporary iteration commit;
- treats an active process as completion;
- edits or commits third-party submodule contents.

ClawPatch owns the findings and repairs. The supervisor owns the reliable journey around them.

## Command reference

```text
--repo PATH                 target Git repository; absolute paths are recommended
--branch current            stay on the checked-out branch
--push none|each|final      local only, push every repair, or push final state
--fresh                     discard only exact checkpoint-owned interrupted work and rebuild the queue
--resume-stopped            resume one exact stopped checkpoint
--timeout-minutes N         watchdog for each ClawPatch child; default 15
--print-state-path          print the external checkpoint/proof directory and exit
--publish-clawpatch-state   explicitly commit safe generated ClawPatch state
```

Exit codes:

- `0`: complete;
- `75`: classified transient stop, suitable for a delayed service restart;
- `2`: terminal, safety, or provenance stop.

## Install from source

For development or a local checkout:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/clawpatch-supervise --version
```

Windows:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\clawpatch-supervise.exe --version
```

## Test and release proof

Run the complete local suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The GitHub workflow runs the full suite, installed CLI smoke test, and native installer on Ubuntu and Windows with Python 3.11 and 3.12. A release is not described as cross-platform until those live jobs pass.

## Project status

Current release: **0.1.6 alpha**.

The state and safety contracts are intentionally strict. If the supervisor cannot prove that a repair, checkpoint, branch, process, or commit belongs to the current finding, it preserves the evidence and refuses to guess.

## Related projects

- [Uncle Matt's Project Manageroo](https://github.com/uncmatteth/Uncle-Matts-Project-Manageroo)
- [ClawPatch on npm](https://www.npmjs.com/package/clawpatch)
- [ClawPatch Supervise on ClawHub](https://clawhub.ai/uncmatteth/skills/clawpatch-supervise)
- [BTT Labs](https://bttlabs.fun)

Built by Uncle Matt at BTT Labs after too many repair queues proved that “the command stopped” and “the work is finished” are not the same sentence.

## License

MIT © 2026 Uncle Matt
