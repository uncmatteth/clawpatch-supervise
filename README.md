# ClawPatch Supervise

> **🤬🦶💥 NEW AND FUCKING IMPROVED — NOW WITH MORE CURSING 🔨🗑️🔥**
>
> Same exact checkpoints, verified repairs, and completion proof. Funnier terminal. Angrier bugs.

> The outside-the-repo supervisor that keeps a ClawPatch repair queue honest, resumable, and moving without skipping the finding that caused trouble.

[![Latest release](https://img.shields.io/github/v/release/uncmatteth/clawpatch-supervise)](https://github.com/uncmatteth/clawpatch-supervise/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

`clawpatch-supervise` runs the long job around [ClawPatch](https://www.npmjs.com/package/clawpatch). ClawPatch still reviews the code, selects the current finding, writes the repair, and revalidates it. This supervisor remembers exactly where the queue was, protects real source progress, prevents unchanged retry loops, commits only verified repair paths, optionally pushes them, and refuses to call the job complete until a fresh review generation proves there is nothing left.

It is a standalone Python package and command-line program. It runs on Linux, macOS, and Windows and has no Python runtime dependencies outside the standard library.

## Quick start

You need Python 3.11 or newer, Git, Node.js 22 or newer with npm, and a provider already authenticated for ClawPatch. Any installed ClawPatch version 0.7.2 or newer is retained. If ClawPatch is missing or older, both installers obtain their release-pinned `clawpatch@0.7.2` package under the isolated install root and verify the resulting command. On Linux and macOS, the installer first downloads the package tarball without running lifecycle scripts, verifies its release-owned SHA-512 integrity pin, and installs only that verified local artifact with lifecycle scripts disabled. On Windows the installer resolves the `.cmd` or `.exe` launcher directly, so PowerShell execution policy cannot select an unusable `.ps1` shim. ClawHub is not a supervisor dependency and is never installed, removed, upgraded, downgraded, or version-pinned by these installers.

### Linux

```bash
git clone https://github.com/uncmatteth/clawpatch-supervise.git
cd clawpatch-supervise
CLAWPATCH_SUPERVISE_SOURCE=. \
  CLAWPATCH_SUPERVISE_VERIFY_REPO=/absolute/path/to/repository \
  ./scripts/install.sh
clawpatch-supervise --version
```

The installer verifies Python 3.11+, Node.js 22+, Git, and compatible ClawPatch before creating installation files. The Linux/macOS compatibility check accepts one semantic version, either bare or prefixed by `clawpatch `, and rejects extra or multiline output. The default release wheel must match its pinned SHA-256 digest before the virtual environment is created. A custom wheel URL or file requires both `CLAWPATCH_SUPERVISE_SOURCE` and its trusted `CLAWPATCH_SUPERVISE_SHA256`; a local source directory can still be installed directly for development. It creates an isolated virtual environment under `~/.local/share/clawpatch-supervise`, requires the staged command to report exactly `clawpatch-supervise VERSION` for the requested package version, and exposes a launcher from `~/.local/bin`. The launcher pins UTF-8 Python I/O, disables Node's compile cache, and keeps the validated Node.js and ClawPatch directories first on `PATH`. When the installer manages ClawPatch, that exact package directory precedes the Node.js directory so an obsolete co-located command cannot shadow it. Supervisor and installer-managed ClawPatch upgrades are staged together and activated only after validation, so a failed candidate leaves the working commands unchanged. After successful activation, superseded installer-managed virtual environments and ClawPatch package roots are retained so commands that started before activation can finish. Remove retained generations only as a separate maintenance action after confirming that no process uses them. Concurrent installers sharing a command directory serialize command inspection, activation, and rollback even when their install roots differ. Command activation fails instead of writing inside an existing directory at either command pathname. If `CLAWPATCH_SUPERVISE_VERIFY_REPO` is set, the installed read-only doctor must prove the target repository and provider before activation completes.

The POSIX installer uses an atomic private lock directory and rejects a preexisting non-directory lock path without following it. It waits at most 30 seconds for another installer, then exits with the lock path and stale-lock recovery instructions instead of hanging forever. Set `CLAWPATCH_SUPERVISE_INSTALL_LOCK_TIMEOUT_SECONDS` to a positive integer to change that wait.

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### macOS

macOS uses the same POSIX installer and runtime contract:

```bash
git clone https://github.com/uncmatteth/clawpatch-supervise.git
cd clawpatch-supervise
CLAWPATCH_SUPERVISE_SOURCE=. \
  CLAWPATCH_SUPERVISE_VERIFY_REPO=/absolute/path/to/repository \
  ./scripts/install.sh
clawpatch-supervise doctor --repo /absolute/path/to/repository
```

### Windows PowerShell

```powershell
git clone https://github.com/uncmatteth/clawpatch-supervise.git
Set-Location clawpatch-supervise
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install.ps1 -Source (Resolve-Path .).Path `
  -VerifyRepo "C:\absolute\path\to\repository" -AddToPath
clawpatch-supervise --version
```

The Windows installer verifies Python 3.11+, Node.js 22+, Git, and ClawPatch 0.7.2 or newer before creating its isolated environment under `%LOCALAPPDATA%\ClawPatchSupervise`. The default wheel is downloaded to a staging directory and must match the release's pinned SHA-256; a custom wheel URL or file requires `-Sha256`, while a local source directory remains available for development. Before publishing its wrapper, the staged command must report exactly `clawpatch-supervise VERSION` for the requested package version. It resolves `.cmd`/`.exe` launchers without invoking PowerShell script shims. Its installed wrapper pins UTF-8 Python I/O, disables Node's compile cache, and puts the verified ClawPatch directory first on `PATH`. `-VerifyRepo` runs the installed read-only doctor and fails installation if Git, the configured provider, or the Windows Codex nested sandbox is not usable. Open a new PowerShell window after `-AddToPath`, or use the printed `.cmd` path immediately.

### Prove a machine without starting a queue

Run this on Windows, Linux, or macOS after authenticating the configured provider:

```bash
clawpatch-supervise doctor --repo /absolute/path/to/repository
```

`doctor` does not create, reset, or advance `.clawpatch`. It first resolves the repository to an existing canonical path, then verifies the Git repository, Python runtime, installed ClawPatch, configured provider, and provider version. On Windows with the Codex provider, it also executes a harmless marker inside each discovered Codex nested sandbox, skips broken duplicate launchers, and passes the first working launcher directory into every later ClawPatch child. If no launcher works, supervision stops before the queue starts.

Configured Windows `.cmd` and `.bat` validation gates are launched with exact `cmd.exe` quoting, including when the executable is installed under a path containing spaces. Executable paths and arguments containing `cmd.exe` metacharacters are rejected, as is a `COMSPEC` launcher that is not a plain `cmd.exe` path.
The initial existing-queue inspection uses the same Windows shim resolution, so a PATH-installed `clawpatch.cmd` works before any repair begins.

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

From a target repository, bare `clawpatch-supervise` uses the current branch, pushes each verified repair, and processes the queue without a start-mode prompt. It resumes any exact stopped checkpoint automatically, preserves an open queue, revalidates every retained uncertain finding, and starts a fresh full review automatically when the existing queue is complete. If `.clawpatch` exists without its required `project.json`, automatic mode stops and preserves the incomplete state; only explicit `--fresh` permits discarding it. Interrupted progress recorded for a different branch is retired automatically, without switching branches or changing source, so stale release-sweep state cannot block supervision on the current branch. When current source cannot be safely resumed from a stopped checkpoint, the external supervisor commits the complete visible tree as an ordinary input-baseline commit on the active branch, records a local baseline ref and receipt, retires the stale wrapper, and reviews that exact current content. Ownerless pre-existing changes use the same baseline contract. Modified, deleted, and untracked files keep their visible content; they are not hidden or restored to an older `HEAD`. Clean divergent local and remote histories are merged automatically with hooks and signing disabled. If an input baseline genuinely conflicts with the remote branch, the baseline remains current and supervision stops for that real Git conflict instead of discarding either side. Another active run also makes the command wait, and transient provider, refusal, quota, or timeout failures retry from durable state every 30 seconds until they succeed or the operator interrupts the command. Use `--push none` to retain verified baseline and repair commits locally. Explicit `--fresh` first commits ownerless current source as the input baseline, then discards the existing ClawPatch queue and rebuilds it while preserving committed ClawPatch configuration. `--resume-stopped` remains available only as a compatibility override that forbids an automatic fresh start.

When divergent-history recovery must align to the remote tree, the supervisor keeps a private `.clawpatch` snapshot outside the worktree, verifies a staged restore before renaming it into place, and retains and reports the snapshot path if restoration fails.

Bare `clawpatch-supervise` uses ClawPatch's finding-level validation and does not run Manageroo's configured full-repository gates, even when the target repository contains `.manageroo/config.toml`. Full project builds and other Manageroo proof gates remain owned by a controlled Manageroo run.

The supervisor resolves `--repo` to one canonical path before preflight and uses that same path for the entire run, so retargeting a repository symlink cannot redirect later repair work.

### Clean up transient run data without deleting receipts

Every supervisor launch owns one private, marked run directory beneath a verified per-user runtime directory when POSIX provides one, otherwise beneath the operating system's temporary directory. Before creating a run, the supervisor rejects a pre-existing cleanup root or per-user parent that is a symlink, belongs to another POSIX user, or is group/world writable. ClawPatch child processes receive the private run directory through `TMPDIR`, `TMP`, and `TEMP`; disposable Python environments and the supervisor's temporary Git indexes and hook directories live there too. The supervisor pins UTF-8 Python I/O and sets `NODE_DISABLE_COMPILE_CACHE=1` because sandboxed Windows Node children can otherwise create an owner-only cache that the parent process cannot traverse or remove. ClawPatch release and fresh-mode state-query children inherit only a small cross-platform operational environment plus validated supervisor-owned overrides, so host provider, cloud, package-registry, and SSH credentials are not forwarded implicitly. Command results and JSON logs redact URL userinfo and named secret query values while retaining the host, path, and non-secret query fields needed for diagnosis. A normal success, stop, validation failure, or keyboard interruption atomically moves the exact owned run into a randomized private quarantine, then removes files, nested directories, the claimed run, and the quarantine through anchored directory descriptors on POSIX or repeated identity checks that refuse reparse-point traversal on Windows. If the operating system denies traversal of a sandbox-owned cache after an otherwise successful run, the supervisor restores that one proven-owned directory, removes the empty quarantine, prints a warning, and still reports the queue result instead of converting completion into `STOPPED`. Direct Python callers that omit `on_blocked_cleanup` instead receive a `SafetyError` identifying the retained run directory.

An abrupt process kill or machine crash can prevent `finally` cleanup. On Linux and Windows, new ownership markers bind the supervisor PID to its process-creation identity when the platform exposes it, so reuse of that numeric PID by an unrelated process does not keep an old run active; legacy markers retain their existing fail-closed PID behavior. The next launch automatically removes a marked run directory only after it is at least one hour old, its recorded process identity no longer matches, and no live process has a working directory or open file inside it. Ownership markers with nonpositive or platform-out-of-range process IDs, or negative or non-finite creation times, are retained as `UNSAFE` before process or age checks. Linux uses `/proc` and retains the directory as `UNSAFE` when any live-process links cannot be inspected; other POSIX systems use a scoped `lsof` probe and retain the directory as `UNSAFE` when that inspection is unavailable, reports a candidate-specific or unclassified diagnostic, or is otherwise inconclusive. Inspect the same decision without changing anything:

```bash
clawpatch-supervise cleanup --dry-run
```

Remove only entries reported as `STALE`:

```bash
clawpatch-supervise cleanup --apply
```

`ACTIVE`, `RECENT`, `UNOWNED`, `UNSAFE`, and `BLOCKED` entries are retained. `BLOCKED` means the directory is proven stale and supervisor-owned but the operating system denied removal; cleanup continues safely instead of crashing preflight. Cleanup never scans or deletes repository `.clawpatch` receipts, standalone checkpoints or completion proofs, dirty source repairs, Git worktrees, or npm/Cargo/Hardhat/uv/GitNexus caches it cannot prove this supervisor run owns. Older unmarked temporary data remains an explicit operator-cleanup decision instead of being guessed away.

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
- external unattended parity with the proven manual loop: commit/push an applied `uncertain`
  repair, continue to the next open finding, and report the retained uncertain count at the end;
- exact-path temporary commits for genuine partial progress;
- same-finding continuation only when ClawPatch produced a new source tree;
- durable checkpoints outside the repository being repaired;
- repository-scoped active-process detection for console, script, and `python -m clawpatch_supervise` launches;
- automatic waiting when another verified run owns the repository;
- a child-process watchdog that escalates surviving timed-out process groups from graceful
  termination to forced termination and proves the group exited before returning;
- automatic in-process retry and exact-checkpoint resume for transient provider, refusal, quota, and timeout failures;
- one marked run-owned temporary root, automatic stale-run pruning, and explicit cleanup dry-run/apply commands;
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
| `open` with a genuinely new source tree | Preserve the iteration locally and re-enter the same finding with the validator's evidence. |
| `uncertain` from the external unattended command | Commit the exact applied repair, push when requested, continue to the next open finding, and revalidate retained uncertain findings during final closure and every later plain run. If no source repair was applied, retain a stopped checkpoint and do not advance. A still-uncertain result remains labeled uncertain. |
| `uncertain` from strict library/Manageroo mode | Preserve the iteration locally and re-enter the same finding with the validator's evidence. |
| `fix` exits `6` after applying source progress | Save the exact repair, run `revalidate` on that repair before another `fix`, finalize it when revalidation says `fixed`, or continue the same finding with new `open` or `uncertain` evidence. |
| `fix` exits `6` without another source diff | Revalidate the code already present. If ClawPatch now proves it `fixed`, continue without demanding a duplicate edit or empty commit. |
| `fix` times out or loses its provider after applying source progress | Save the exact repair and revalidate it before spending another provider attempt; finalize immediately when the saved repair is already fixed. |
| Other validation/provider failure after new source progress | Preserve the exact progress and continue the same finding. |
| Configured project gate is red while resuming an exact stopped `open` or `uncertain` repair | Preserve the checkpoint and re-enter only that finding with the exact gate failure as repair evidence. |
| A legacy checkpoint includes fingerprinted untracked `node_modules` install output beside an exact applied repair | Keep the dependency tree in place, normalize ownership to the applied repair paths, and resume. Tracked dependency files remain source. |
| `false-positive` | Restore only the exact supervisor-owned repair paths to the finding's starting tree, retire the checkpoint, continue. |
| Open or uncertain revalidation with no new source changes, including after a checkpointed repair | Feed the new evidence into up to two more same-finding attempts; never advance the queue or try to save the identical tree again. |
| Same finding still returns the same tree after bounded recovery | Preserve the checkpoint and stop without losing source. |
| Transient provider, refusal, quota, or timeout with no source progress | Wait, retry automatically, and resume the exact durable checkpoint or source-clean command without requiring a new invocation. |
| Dirty source no longer matches a stale stopped checkpoint | Commit the complete current tree as the active input baseline, retain its visible content, record a baseline receipt, retire the stale checkpoint, and continue. |
| Pre-existing source remains with no checkpoint owner in a bare external run | Commit the complete changed tree as the active input baseline, retain `.clawpatch`, and review the current content. |
| Conflicting clean local and remote histories | Preserve local history under a local-only recovery ref, align the active branch to stable origin, restore `.clawpatch`, and continue. |
| A current input baseline conflicts with the remote branch | Keep the baseline current and stop on the real Git conflict; never replace it with remote source. |
| Detached, missing, or moving Git state | Exit `2` and leave the evidence for inspection. |
| External run reaches zero open findings with uncertain findings retained | Write `status: PROCESSED_WITH_UNCERTAIN`, include the exact uncertain count, retain `.clawpatch`, and exit `0` without claiming those findings are fixed. |
| Strict final review proves zero remaining work | Write `status: COMPLETE` proof, retain `.clawpatch` so `clawpatch status` remains verifiable, and exit `0`. |

## Built for real repositories

Real repair queues run into restarts, provider failures, overlapping findings, nested repositories, and long validation jobs. ClawPatch Supervise keeps those cases inside one visible, verifiable workflow:

- **Repository ownership stays clear.** Fresh runs exclude Git submodules and their descendants from ClawPatch review, so the queue repairs code owned by the target repository instead of editing a dependency checkout.
- **Checkpoints are exact.** Every stopped repair is bound to its repository, branch, finding, starting commit, owned paths, and source fingerprint.
- **Restarts continue safely.** A later applied repair is resumed only when its finding, base commit, and complete current source-path set match the checkpoint boundary. If a stale checkpoint names an older finding, the plain external command may rebind it to exactly one newer applied attempt only when the complete dirty path set, preserved attempt base, attempt time, and an independent `clawpatch show` result all agree. Zero or multiple matches wait without adopting or touching the files.
- **Stale checkpoint wrappers recover into the current tree.** When a stopped checkpoint no longer proves the current source changes, the external command commits every current changed source path as the active input baseline, writes a receipt that records the stale checkpoint boundary, retires only the wrapper, and reviews that exact tree. It does not hide the files or restore older content.
- **Ownerless source does not create an infinite wait.** If a bare external run finds source changes and no durable checkpoint owns them, it commits the exact complete changed tree as the input baseline, records a local baseline ref and external receipt, leaves `.clawpatch` untouched, and continues. Strict embedded Manageroo calls still reject pre-existing source instead of changing it.
- **Published updates do not strand the next plain run.** When pushing is enabled and the current branch has no source changes, a strictly behind local HEAD is fast-forwarded to the stable live origin commit with local Git hooks disabled. A clean strictly ahead local HEAD is accepted for the authorized push path. Clean divergence is merged when possible; a conflicting merge preserves local history under a recovery ref, follows stable origin, restores the exact existing `.clawpatch` state, and continues. Detached, missing, or moving branches still stop safely.
- **A rejected push does not invalidate its finished repair.** If a restart finds a `finalized` checkpoint and the current history contains one commit whose complete source-path set exactly matches that checkpoint, the supervisor retires only the checkpoint, keeps the committed repair, and continues to remote reconciliation. Uncommitted source or a path mismatch still prevents automatic recovery.
- **Supervisor-owned failures recover at one boundary.** The plain external command no longer makes the operator repair its checkpoint or queue by hand. It commits any complete visible source tree as the next input baseline, preserves a malformed checkpoint verbatim in the external recovery directory, retires only active supervisor state, rebuilds the ClawPatch queue, and reviews again. A source-clean broken queue receives one automatic rebuild per Git/error boundary; if the exact same boundary fails again, it is reported as a real external failure instead of looping forever.
- **Empty stop markers do not strand open findings.** If a stopped checkpoint owns no source, has no temporary commit, and ClawPatch still reports the exact finding open at the same HEAD, the supervisor retires only that empty wrapper. `clawpatch next` must return the same finding before its fix is attempted again.
- **Finished release work does not strand the queue.** If Git HEAD cleanly advances from a checkpoint's base and its temporary iteration commit still proves the same finding, the supervisor accepts both a retained dangling iteration and an iteration already included in that clean history, then retires only the obsolete recovery wrapper. Unrelated dirty paths do not keep that obsolete wrapper alive; they remain untouched and are handled as independent pre-existing work. The same recovery applies when a source-clean checkpoint owns no paths and has no temporary commit, because there is no repair content to lose. It preserves `.clawpatch` and lets ClawPatch select that finding or the next one normally.
- **Reset-capable database tests stay disposable.** A detected PostgreSQL test contract must declare an official `postgres@sha256:<64 lowercase hex characters>` image and always receives a newly owned loopback-only database from that exact immutable artifact. The sanitized environment is passed exactly so inherited database credentials and reset guards stay removed from ClawPatch child processes. After successful startup, cleanup uses the exact generated container name even if Docker returns malformed container-ID output.
- **Python provisioning cannot run repository build hooks on the host.** Repositories with target-declared dependencies fail closed while no OS-enforced provisioning sandbox is available. Dependency-free projects receive only wheel-distributed pytest in a sanitized disposable venv; the target project is never installed, and a venv-local path file exposes its source only to the validation interpreter without putting repository paths in the ClawPatch control process environment.
- **New evidence gets another chance in strict mode.** Open and uncertain revalidations retry through the bounded read-only, workspace-write, and authorized trusted-host ladder. The external unattended command instead records an applied uncertain repair and continues the remaining open queue, matching the documented manual workflow without falsely relabeling the result as fixed.
- **Bare invocation owns recovery.** The default command resumes the current `.clawpatch` queue and exact stopped checkpoint, snapshots and clears ambiguous stopped-checkpoint source without data loss, revalidates retained uncertain findings, reconciles clean divergent Git histories without losing the local commit or queue state, waits for another active owner, and retries transient failures without exiting. It automatically resets only a proven-clean queue with clean project source.
- **State queries keep JSON clean.** Existing-queue checks parse only command stdout; stderr diagnostics remain separate and are included with bounded stdout context when a query fails.
- **Zero heuristic features are not completion.** The supervisor asks ClawPatch's own agent mapper to inspect the repository before accepting an empty map, which keeps Solidity and other unsupported heuristic languages in the real review flow.
- **Exit 6 means revalidate, not give up.** When `clawpatch fix` applies a repair but its own validation exits `6`, the supervisor checkpoints that repair and revalidates it before deciding whether another fix is necessary. If the repair was already present and a later `fix` has nothing new to edit, the supervisor revalidates that existing code instead of misclassifying the empty second diff as the repair disappearing.
- **False positives clean themselves up.** Only the exact supervisor-owned repair paths are restored; unrelated work is left alone.
- **Completion stays inspectable.** The command exits successfully only after the queue is empty and a fresh review generation finds nothing else to repair, and it keeps `.clawpatch` so the result can still be checked with `clawpatch status --json`.
- **Transient cleanup is ownership-gated.** Normal runs remove their exact marked temporary root. Relaunch and manual cleanup remove only old dead runs with no proven live reference; receipts, repairs, unknown worktrees, and unowned caches are preserved.

The terminal shows these transitions directly, including `RESUME APPLIED REPAIR`, the current finding, the owned files, watchdog time, commit, push, and final proof. Heartbeat fields, directly printed repository, state, and cleanup paths, and redacted exception messages escape terminal control characters as visible text. A failed sweep prints `STOPPED` with the remaining open-finding count; only a successful sweep prints `COMPLETE` and `QUEUE'S CLEAN`.

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
legacy mismatch never grants ownership to changed source; the external supervisor instead commits
the complete current tree as its new input baseline before retiring the stale wrapper.
For a stopped checkpoint that no longer proves the current dirty tree, every changed source path
is included in the active baseline commit with a receipt in the external state directory. Visible
file content stays in place, and the existing queue continues by reviewing that exact baseline.

Repeated local iterations can create different temporary commit IDs for the same repair. Resume
accepts an older attempt boundary only when its finding, parent, owned paths, and complete Git tree
independently match the checkpoint; a merely similar or different repair remains a safety stop.

Manual progress can also supersede a stopped wrapper with a newer finding. The plain external
command scans recorded patch attempts only to identify candidates, requires exactly one applied
attempt whose complete file set is the current dirty set and whose base still preserves those
paths, rejects files edited after that attempt, and then verifies the same record through
`clawpatch show`. Only that proof can replace the stale finding ID. Ambiguous, unrelated, malformed,
or unconfirmed state remains untouched while the process waits and retries.

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
    --timeout-minutes 15
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
  -ArgumentList @("--repo", $Repo, "--branch", "current", "--push", "each", "--timeout-minutes", "15") `
  -RedirectStandardOutput "$Logs\queue.out.log" `
  -RedirectStandardError "$Logs\queue.err.log" `
  -WindowStyle Hidden

Get-Content "$Logs\queue.out.log" -Wait
```

For unattended restart after sign-in or reboot, create a Windows Task Scheduler task that invokes the same installed `.exe` and arguments. The process handles transient retries itself; exit `2` remains a terminal safety/provenance stop that needs inspection.

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
--fresh                     discard the existing queue and start a fresh full review
--resume-stopped            resume existing state or one exact stopped checkpoint
--timeout-minutes N         watchdog for each ClawPatch child; default 15
--retry-seconds N           positive wait before an automatic transient/busy retry; default 30
--print-state-path          print the external checkpoint/proof directory and exit
--publish-clawpatch-state   explicitly commit safe generated ClawPatch state
```

Exit codes:

- `0`: complete;
- `75`: classified transient stop outside the normal in-process retry boundary;
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

Run the complete offline local suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The installed-wheel console-entry-point integration test intentionally uses isolated PEP 517 build
requirements from the package index. It imports the installed package from a clean virtual
environment and exercises the generated executable's version, help, doctor, cleanup dry-run,
state-path, and argument-error modes. Run that explicit network lane separately:

```bash
CLAWPATCH_SUPERVISE_RUN_NETWORK_TESTS=1 PYTHONPATH=src python3 -m unittest \
  tests.test_installed_cli.InstalledConsoleScriptTests.test_clawpatch_supervise_entrypoint_from_installed_wheel -v
```

Its setuptools and wheel versions are pinned identically in `pyproject.toml` and
`requirements-test.txt`, so an index outage can affect only this opt-in lane and a future backend
release cannot silently change the build.

This repository intentionally has no hosted workflow files. Run the real build/install lane and
the full suite manually on Linux, macOS, and Windows before describing a release as
cross-platform, and retain the command output as local release evidence.

The release version is declared once as `clawpatch_supervise.__version__`; setuptools derives distribution metadata from that attribute, and the CLI reports the same value.

## Project status

Current release: **0.1.34 alpha**.

The state and safety contracts are intentionally strict. If the supervisor cannot prove that a repair, checkpoint, branch, process, or commit belongs to the current finding, it preserves the evidence and refuses to guess.

## Related projects

- [Uncle Matt's Project Manageroo](https://github.com/uncmatteth/Uncle-Matts-Project-Manageroo)
- [ClawPatch on npm](https://www.npmjs.com/package/clawpatch)
- [BTT Labs](https://bttlabs.fun)

Built by Uncle Matt at BTT Labs because ClawPatch is fucking awesome, AI agents leave garbage behind, and copying repair commands all night is bullshit.

## License

MIT © 2026 Uncle Matt
