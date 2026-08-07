# ClawPatch Supervise

> **🤬🦶💥 NEW AND FUCKING IMPROVED — NOW WITH MORE CURSING 🔨🗑️🔥**
>
> Same exact checkpoints, verified repairs, and completion proof. Funnier terminal. Angrier bugs.

> The outside-the-repo supervisor that keeps a ClawPatch repair queue honest, resumable, and moving without skipping the finding that caused trouble.

[![Cross-platform tests](https://github.com/uncmatteth/clawpatch-supervise/actions/workflows/test.yml/badge.svg)](https://github.com/uncmatteth/clawpatch-supervise/actions/workflows/test.yml)
[![Latest release](https://img.shields.io/github/v/release/uncmatteth/clawpatch-supervise)](https://github.com/uncmatteth/clawpatch-supervise/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

`clawpatch-supervise` runs the long job around [ClawPatch](https://www.npmjs.com/package/clawpatch). ClawPatch still reviews the code, selects the current finding, writes the repair, and revalidates it. This supervisor remembers exactly where the queue was, protects real source progress, prevents unchanged retry loops, commits only verified repair paths, optionally pushes them, and refuses to call the job complete until a fresh review generation proves there is nothing left.

It is a standalone Python package, a command-line program, and a public [ClawHub skill](https://clawhub.ai/uncmatteth/skills/clawpatch-supervise). It runs on Linux, macOS, and Windows and has no Python runtime dependencies outside the standard library.

## Quick start

You need Python 3.11 or newer, Git, Node/npm, and a provider already authenticated for ClawPatch. If the `clawpatch` command is missing, either installer below installs the reviewed `clawpatch@0.7.2` release globally with npm and verifies the command. An existing ClawPatch installation is left unchanged; ClawPatch 0.7.2 or newer is required.

### Linux and macOS

```bash
git clone https://github.com/uncmatteth/clawpatch-supervise.git
cd clawpatch-supervise
./scripts/install.sh
clawpatch-supervise --version
```

The installer verifies that the selected Python interpreter is 3.11 or newer and that ClawHub is available or installable before creating any installation files. It then creates an isolated virtual environment under `~/.local/share/clawpatch-supervise`, verifies the installed commands, and exposes them from `~/.local/bin`. If ClawHub is missing, it also installs the reviewed `clawhub@0.19.1` CLI into that isolated root. An existing ClawHub installation is preserved. If that directory is not already on `PATH`, reopen your terminal or run:

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

The Windows installer creates an isolated environment under `%LOCALAPPDATA%\ClawPatchSupervise`. If ClawHub is missing, it also installs the reviewed `clawhub@0.19.1` CLI into that isolated root and creates `clawhub.cmd`; an existing installation is preserved. Open a new PowerShell window after `-AddToPath`, or use the printed `.cmd` paths immediately.

Configured Windows `.cmd` and `.bat` validation gates are launched with exact `cmd.exe` quoting, including when the executable is installed under a path containing spaces. Gate arguments containing `cmd.exe` metacharacters remain rejected.

### Run a queue

Linux or macOS:

```bash
clawpatch-supervise \
  --repo /absolute/path/to/your/repository \
  --branch current \
  --push each \
  --timeout-minutes 15
```

Windows PowerShell:

```powershell
clawpatch-supervise `
  --repo "C:\absolute\path\to\your\repository" `
  --branch current `
  --push each `
  --timeout-minutes 15
```

Use `--push none` if you want verified local commits without publishing them. The normal command preserves and processes an existing `.clawpatch` queue instead of deleting it. When an existing queue is proven clean and project source is clean, an interactive run asks whether to remove that state and start a new full review. `--fresh` is the explicit non-interactive reset choice, and it refuses to reset while any project source is dirty. Use `--resume-stopped` for an exact stopped supervisor checkpoint.

## Why I made this

I made this because ClawPatch is fucking awesome at finding garbage bugs that AI agents leave behind, but operating a long repair queue by hand sucks. I do not want to decode every internal step or copy and paste `next`, `show`, `fix`, and `revalidate` commands over and over just to make sure the same broken finding does not get skipped. Nobody else should have to babysit that shit either.

You point `clawpatch-supervise` at a repository. It runs the queue, shows what it is doing in plain language, remembers the exact finding and source changes, keeps working when ClawPatch produces real progress, and stops safely when the evidence does not justify another move. The cursing is personality; the commands, paths, finding IDs, commits, errors, and proof stay exact.

On a real repository, a repair can take several attempts. A provider can time out after changing source. Validation can fail after the first useful edit. Revalidation can reopen the same finding. A process can die with a good partial repair still sitting in the worktree. If you blindly run the same command again, you can loop forever. If you call `clawpatch next`, you can abandon the exact finding that still needs to be resolved. If you let a chat session remember the queue, context compaction or a closed terminal can erase the only copy of what happened.

I ran into those failures while using ClawPatch on Manageroo and other actual projects. I needed something outside the target repository that could answer five boring but critical questions every time:

1. Which exact finding owns these changes?
2. Is this a genuinely new source tree or the same failed attempt again?
3. Can this repair be committed without pulling unrelated work into it?
4. If the process stops, can the next process prove exactly what it is resuming?
5. Did the entire queue really finish, including a fresh final review, or did the terminal merely stop printing?

That is what this program does. It is deliberately more stubborn than a shell loop and less creative than an AI agent. It follows the command-owned lifecycle, records durable evidence, and stops only when continuing would mean guessing, losing work, or overstating completion.

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
- automatic ClawPatch agent mapping when the heuristic mapper reports zero features, so unsupported languages such as Solidity are not falsely declared empty;
- one-current-finding `next → show → fix → revalidate` ordering;
- exact-path temporary commits for genuine partial progress;
- same-finding continuation only when ClawPatch produced a new source tree;
- durable checkpoints outside the repository being repaired;
- repository-scoped active-process detection for console, script, and `python -m clawpatch_supervise` launches;
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
| `open` or `uncertain` with a genuinely new source tree | Preserve the iteration locally and re-enter the same finding with the validator's evidence. |
| `fix` exits `6` after applying source progress | Save the exact repair, run `revalidate` on that repair before another `fix`, finalize it when revalidation says `fixed`, or continue the same finding with new `open` or `uncertain` evidence. |
| Other validation/provider failure after new source progress | Preserve the exact progress and continue the same finding. |
| `false-positive` | Restore only the exact supervisor-owned repair paths to the finding's starting tree, retire the checkpoint, continue. |
| Open revalidation with no source changes | Feed the new evidence into up to two more same-finding attempts; never advance the queue. |
| Same finding still returns the same tree after bounded recovery | Preserve the checkpoint and stop without losing source. |
| Transient provider, refusal, quota, or timeout with no source progress | Exit `75` so a service may restart later. |
| Ownership, branch, checkpoint, or Git provenance mismatch | Exit `2` and leave the source for inspection. |
| Fresh final review proves zero remaining work | Write `status: COMPLETE` proof, retain `.clawpatch` so `clawpatch status` remains verifiable, and exit `0`. |

## Built for real repositories

Real repair queues run into restarts, provider failures, overlapping findings, nested repositories, and long validation jobs. ClawPatch Supervise keeps those cases inside one visible, verifiable workflow:

- **Repository ownership stays clear.** Fresh runs exclude Git submodules and their descendants from ClawPatch review, so the queue repairs code owned by the target repository instead of editing a dependency checkout.
- **Checkpoints are exact.** Every stopped repair is bound to its repository, branch, finding, starting commit, owned paths, and source fingerprint.
- **Restarts continue safely.** A later applied repair is resumed only when its finding, base commit, and complete current source-path set match the checkpoint boundary.
- **Finished release work does not strand the queue.** If Git HEAD cleanly advances from a stopped checkpoint's base and its temporary iteration commit still proves the same finding, the supervisor retires only the obsolete recovery wrapper. It preserves `.clawpatch` and lets ClawPatch select that finding or the next one normally.
- **Reset-capable database tests stay disposable.** A detected PostgreSQL test contract always receives a newly owned loopback-only database, and inherited database credentials and reset guards are removed from ClawPatch child processes. After successful startup, cleanup uses the exact generated container name even if Docker returns malformed container-ID output.
- **New evidence gets another chance.** Open and uncertain revalidations retry through the bounded read-only, workspace-write, and authorized trusted-host ladder. When either result belongs to a genuinely changed repair tree, its concrete failures feed the next same-finding fix instead of stopping the queue. An unchanged uncertain result still stops rather than looping blindly.
- **Existing queues do not get erased.** The default command resumes the current `.clawpatch` queue. A reset is offered only for a proven-clean queue with clean project source, and dirty source blocks reset.
- **Zero heuristic features are not completion.** The supervisor asks ClawPatch's own agent mapper to inspect the repository before accepting an empty map, which keeps Solidity and other unsupported heuristic languages in the real review flow.
- **Exit 6 means revalidate, not give up.** When `clawpatch fix` applies a repair but its own validation exits `6`, the supervisor checkpoints that repair and revalidates it before deciding whether another fix is necessary.
- **False positives clean themselves up.** Only the exact supervisor-owned repair paths are restored; unrelated work is left alone.
- **Completion stays inspectable.** The command exits successfully only after the queue is empty and a fresh review generation finds nothing else to repair, and it keeps `.clawpatch` so the result can still be checked with `clawpatch status --json`.

The terminal shows these transitions directly, including `RESUME APPLIED REPAIR`, the current finding, the owned files, watchdog time, commit, push, and final proof. A failed sweep prints `STOPPED` with the remaining open-finding count; only a successful sweep prints `COMPLETE` and `QUEUE'S CLEAN`.

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

- Linux and macOS: `${XDG_STATE_HOME:-~/.local/state}/clawpatch-supervise`
- Windows: `%LOCALAPPDATA%\ClawPatchSupervise\state`

`--resume-stopped` accepts only an exact matching checkpoint. It does not mean “ignore the last error.”

When a checkpoint moves from an older Manageroo installation, the supervisor upgrades its
source fingerprint only after the legacy algorithm still proves the exact current files. A
mismatch remains a safety stop; migrated state never grants ownership to changed source.

Repeated local iterations can create different temporary commit IDs for the same repair. Resume
accepts an older attempt boundary only when its finding, parent, owned paths, and complete Git tree
independently match the checkpoint; a merely similar or different repair remains a safety stop.

If later committed release work leaves the source tree clean, a verified stopped checkpoint may
become only a stale recovery wrapper. The supervisor retires that wrapper automatically only when
the current HEAD descends from its recorded base, its temporary commit is still a valid iteration
for the same finding, and the finding remains in `.clawpatch`. The queue itself is never deleted or
advanced by this recovery.

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
--fresh                     explicitly reset only clean project source and rebuild the queue
--resume-stopped            resume existing state or one exact stopped checkpoint
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

The GitHub workflow runs the full suite, installed CLI smoke test, and native installer on Ubuntu, macOS, and Windows with Python 3.11 and 3.12. A release is not described as cross-platform until those live jobs pass.

## Project status

Current release: **0.1.12 alpha**.

The state and safety contracts are intentionally strict. If the supervisor cannot prove that a repair, checkpoint, branch, process, or commit belongs to the current finding, it preserves the evidence and refuses to guess.

## Related projects

- [Uncle Matt's Project Manageroo](https://github.com/uncmatteth/Uncle-Matts-Project-Manageroo)
- [ClawPatch on npm](https://www.npmjs.com/package/clawpatch)
- [ClawPatch Supervise on ClawHub](https://clawhub.ai/uncmatteth/skills/clawpatch-supervise)
- [BTT Labs](https://bttlabs.fun)

Built by Uncle Matt at BTT Labs because ClawPatch is fucking awesome, AI agents leave garbage behind, and copying repair commands all night is bullshit.

## License

MIT © 2026 Uncle Matt
