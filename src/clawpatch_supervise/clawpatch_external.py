from __future__ import annotations

import argparse
import sys
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .clawpatch_protocol import RepairAction
from .clawpatch_release import (
    CLAWPATCH_CHILD_WATCHDOG_SECONDS,
    ClawpatchCommandFailure,
    ClawpatchStop,
    _parse_json_output,
    _source_paths,
    external_state_root,
    release_sweep,
    require_external_clawpatch_preflight,
)
from .cleanup import cleanup_owned_runs, owned_run_directory
from .errors import SafetyError
from .runner import CommandRunner
from .validation_services import provision_disposable_validation_environment


def _clawpatch_state_exists(repo: Path) -> bool:
    state = repo.resolve() / ".clawpatch"
    return state.is_dir() and (state / "project.json").is_file()


def _run_state_query(repo: Path, argv: list[str]) -> dict[str, Any]:
    result = CommandRunner().run(
        argv,
        cwd=repo,
        timeout_seconds=120,
        kill_process_group=True,
    )
    if result.exit_code != 0:
        raise SafetyError(
            "Could not prove whether existing ClawPatch state is clean; preserving it.\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    return _parse_json_output(result.stdout, command=" ".join(argv[1:]))


def _existing_queue_is_clean(repo: Path) -> bool:
    status = _run_state_query(repo, ["clawpatch", "status", "--json"])
    for field in ("openFindings", "activeLocks", "lockFiles"):
        value = status.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SafetyError(f"Existing ClawPatch status has an invalid {field!r} value.")
    if any(status[field] for field in ("openFindings", "activeLocks", "lockFiles")):
        return False
    uncertain = _run_state_query(repo, ["clawpatch", "report", "--status", "uncertain", "--json"])
    total = uncertain.get("total")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise SafetyError("Existing ClawPatch uncertain report has an invalid total.")
    return total == 0


def _resolve_fresh_mode(repo: Path, requested: bool | None) -> bool:
    if requested is False:
        return False
    if not _clawpatch_state_exists(repo):
        return True
    if not _existing_queue_is_clean(repo):
        if requested is True:
            raise SafetyError("Explicit --fresh requires an existing ClawPatch queue to be clean.")
        return False
    if _source_paths(repo):
        if requested is True:
            raise SafetyError("Explicit --fresh refuses to discard retained project source changes.")
        return False
    if requested is True:
        return True
    if not sys.stdin.isatty():
        print(
            "Existing clean .clawpatch state retained; use --fresh to explicitly start over.",
            flush=True,
        )
        return False
    try:
        answer = input(
            "ClawPatch queue is clean. Remove .clawpatch and start a new full review? [y/N] "
        )
    except EOFError as exc:
        raise SafetyError("Fresh-state prompt closed; existing .clawpatch state retained.") from exc
    return answer.strip().casefold() in {"y", "yes"}


def _terminal_safe(value: Any) -> str:
    text = str(value)
    escaped = []
    for character in text:
        codepoint = ord(character)
        if character == "\n":
            escaped.append(r"\n")
        elif character == "\r":
            escaped.append(r"\r")
        elif character == "\t":
            escaped.append(r"\t")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            escaped.append(f"\\x{codepoint:02x}")
        else:
            escaped.append(character)
    return "".join(escaped)


def _counter(event: dict[str, Any]) -> str:
    current = _terminal_safe(event.get("current", "?"))
    total = _terminal_safe(event.get("total", "?"))
    return f"[{current}/{total}]"


def _render_inspection(event: dict[str, Any]) -> str:
    inspection = event.get("inspection")
    finding = inspection.get("finding") if isinstance(inspection, dict) else None
    if not isinstance(finding, dict):
        return f"{_counter(event)} SHOW {_terminal_safe(event.get('finding_id', ''))}"
    lines = [
        "",
        f"{_counter(event)} SHOW — 🔎🐛🗑️ LOOK AT THIS FUCKING THING",
        f"$ {_terminal_safe(event.get('command', ''))}",
        f"title: {_terminal_safe(finding.get('title', ''))}",
        f"id: {_terminal_safe(finding.get('id', ''))}",
        f"severity: {_terminal_safe(finding.get('severity', ''))}",
        f"category: {_terminal_safe(finding.get('category', ''))}",
    ]
    evidence = finding.get("evidence")
    if isinstance(evidence, list) and evidence:
        lines.append("evidence:")
        for item in evidence:
            if not isinstance(item, dict):
                continue
            start = item.get("startLine")
            end = item.get("endLine")
            location = _terminal_safe(item.get("path", ""))
            if isinstance(start, int):
                location += f":{start}"
                if isinstance(end, int) and end != start:
                    location += f"-{end}"
            symbol = item.get("symbol")
            if symbol:
                location += f" ({_terminal_safe(symbol)})"
            lines.append(f"- {location}")
    for label, field in (
        ("reproduction", "reproduction"),
        ("recommendation", "recommendation"),
        ("minimum fix scope", "minimumFixScope"),
    ):
        value = finding.get(field)
        if value:
            lines.extend([f"{label}:", _terminal_safe(value)])
    validation = inspection.get("validation") if isinstance(inspection, dict) else None
    if isinstance(validation, list) and validation:
        lines.extend(["validation:", *[f"- {_terminal_safe(command)}" for command in validation]])
    return "\n".join(lines)


def _render_event(event: dict[str, Any]) -> str:
    phase = event.get("phase")
    command_phases = {
        "preflight": "PROCESS PREFLIGHT",
        "fresh": "FRESH INIT",
        "fresh-discard": "FRESH OWNED CLEANUP",
        "init": "INIT",
        "status": "STATUS",
        "lock-cleanup": "LOCK CLEANUP",
        "baseline-validation": "BASELINE VALIDATION",
        "validation-environment-start": "VALIDATION ENVIRONMENT START",
        "validation-service-start": "VALIDATION SERVICE START",
        "map": "MAP",
        "map-agent": "MAP WITH CLAWPATCH AGENT",
        "review": "REVIEW",
        "review-verification": "REVIEW VERIFICATION",
        "queue": "QUEUE",
        "show": "SHOW",
        "revalidate": "REVALIDATE",
        "revalidate-escalated": "REVALIDATE ESCALATED",
        "revalidate-host": "REVALIDATE TRUSTED HOST",
        "uncertain-revalidation": "UNCERTAIN REVALIDATION",
        "commit": "COMMIT",
        "push": "PUSH",
        "report": "REPORT",
        "state-cleanup": "CLAWPATCH STATE CLEANUP",
        "state-retained": "CLAWPATCH STATE RETAINED",
    }
    if phase in command_phases:
        attempt = event.get("attempt")
        maximum = event.get("max_attempts")
        suffix = (
            f" (attempt {_terminal_safe(attempt)}/{_terminal_safe(maximum)})"
            if attempt and maximum
            else ""
        )
        personality = {
            "preflight": " — 🥾🔍 CHECK THE DAMN TOOLS",
            "fresh": " — 🌱🤬 START THIS SHIT CLEAN",
            "fresh-discard": " — 🧹💥 REMOVE ONLY OUR OLD SHIT",
            "baseline-validation": " — 🧪🧱 PROVE THE REPO ISN'T ALREADY FUCKED",
            "map": " — 🗺️🔍 FIND ALL THE SHIT",
            "map-agent": " — 🧠🔍 HEURISTIC FOUND FUCK-ALL; USE THE AGENT",
            "review": " — 🧐🗑️ HUNTING GARBAGE",
            "queue": " — 📋🤬 LINE UP THE BUGS",
            "revalidate": " — 🧪👀 DID THAT SHIT ACTUALLY WORK?",
            "revalidate-escalated": " — 🧪😤 CHECK IT AGAIN, DAMMIT",
            "revalidate-host": " — 🧪💻 CHECK IT OUTSIDE THE SANDBOX BULLSHIT",
            "uncertain-revalidation": " — 🤨🤔 THIS SHIT NEEDS ANOTHER LOOK",
            "commit": " — 📦🔒 LOCKING IN THE FIX",
            "push": " — 🚀🔥 SHIP THE FIXED SHIT",
            "state-cleanup": " — 🧹🗑️ CLEAN UP THE RUNTIME CRAP",
            "state-retained": " — 🔒📋 KEEP THE FUCKING RECEIPTS",
        }.get(str(phase), "")
        return (
            f"\n{_counter(event)} {command_phases[str(phase)]}{suffix}{personality}\n"
            f"$ {_terminal_safe(event.get('command', ''))}"
        )
    if phase == "finding":
        return _render_inspection(event)
    if phase == "false-positive":
        return (
            f"\n{_counter(event)} FALSE-POSITIVE — 🙄🗑️ BOGUS BUG. "
            "THROW OUT ONLY OUR SHIT AND KEEP MOVING\n"
            f"finding: {_terminal_safe(event.get('finding_id', ''))}\n"
            f"detail: {_terminal_safe(event.get('detail', ''))}"
        )
    if phase == "reset-recovery":
        return (
            f"\n{_counter(event)} CHECKPOINT RECOVERY — "
            "🧹🔧 CLEANING UP INTERRUPTED SHIT SAFELY\n"
            f"finding: {_terminal_safe(event.get('finding_id', ''))}\n"
            f"$ {_terminal_safe(event.get('command', ''))}"
        )
    if phase == "submodule-exclusion":
        return (
            f"\n{_counter(event)} SUBMODULE EXCLUSION — "
            "🚧🙅 NOT TOUCHING SOMEBODY ELSE'S SHIT\n"
            f"excluded: {_terminal_safe(event.get('detail', ''))}"
        )
    if phase == "validation-service-ready":
        return f"\n{_counter(event)} VALIDATION SERVICE READY\n$ {_terminal_safe(event.get('detail', ''))}"
    if phase == "validation-service-cleanup":
        return f"\n{_counter(event)} VALIDATION SERVICE CLEANUP\n$ {_terminal_safe(event.get('detail', ''))}"
    if phase == "validation-environment-ready":
        return f"\n{_counter(event)} VALIDATION ENVIRONMENT READY\n$ {_terminal_safe(event.get('detail', ''))}"
    if phase == "validation-environment-cleanup":
        return f"\n{_counter(event)} VALIDATION ENVIRONMENT CLEANUP\n$ {_terminal_safe(event.get('detail', ''))}"
    if phase == "fix":
        attempt = int(event.get("attempt", 1))
        maximum = event.get("max_attempts")
        if maximum:
            suffix = f" (attempt {attempt}/{_terminal_safe(maximum)})"
        else:
            suffix = f" (attempt {attempt})" if attempt > 1 else ""
        return (
            f"\n{_counter(event)} FIX{suffix} — 🔨🤬🦶 KICK THIS BUG'S ASS\n"
            f"$ {_terminal_safe(event.get('command', ''))}"
        )
    if phase == "stopped":
        owned = event.get("owned_paths")
        paths = (
            ", ".join(_terminal_safe(path) for path in owned)
            if isinstance(owned, list)
            else ""
        )
        return (
            f"\n{_counter(event)} STOPPED - "
            f"{_terminal_safe(event.get('outcome', 'not fixed'))} — "
            "🛑💥🤬 FUCK. THIS SHIT ISN'T SAFE TO ADVANCE\n"
            f"finding: {_terminal_safe(event.get('finding_id', ''))}\n"
            f"source left in place: {paths or 'none'}"
        )
    if phase == "fixed":
        commit = event.get("commit") or "no source commit required"
        return (
            f"\n{_counter(event)} FIXED — 🔥🔨 FUCK YES, THIS SHIT'S FIXED\n"
            f"commit: {_terminal_safe(commit)}"
        )
    if phase == "continuing":
        commit = event.get("commit") or "no source commit required"
        return (
            f"\n{_counter(event)} MOTHERFUCKER, SHIT'S STILL FUCKED. "
            "CONTINUING THE SAME FUCKING FINDING. 🤬🦶💥\n"
            f"commit: {_terminal_safe(commit)}"
        )
    if phase == "fixed-point-rescan":
        generation = event.get("attempt", "?")
        return (
            f"\n{_counter(event)} FRESH FIXED-POINT REVIEW "
            f"(generation {_terminal_safe(generation)}) — "
            "🕵️🗑️ CHECKING FOR MORE GARBAGE\n"
            f"$ {_terminal_safe(event.get('command', ''))}"
        )
    if phase == "resume":
        owned = event.get("owned_paths")
        if isinstance(owned, list) and owned:
            return (
                f"\n{_counter(event)} RESUME APPLIED REPAIR — "
                "😤🔧 FOUND THE SAVED FIX\n"
                f"finding: {_terminal_safe(event.get('finding_id', ''))}\n"
                f"source changes: {', '.join(_terminal_safe(path) for path in owned)}"
            )
        return (
            f"\n{_counter(event)} RESUME INTERRUPTED PLANNED ATTEMPT — "
            "🧟🔧 PICKING THIS SHIT BACK UP\n"
            f"finding: {_terminal_safe(event.get('finding_id', ''))}\n"
            "source changes: none; returning through ClawPatch next"
        )
    detail = event.get("detail") or event.get("command") or phase or "working"
    return f"{_counter(event)} {_terminal_safe(detail).upper()}"


def main(
    argv: list[str] | None = None,
    *,
    run_sweep: Callable[..., dict[str, Any]] = release_sweep,
    provision_validation_environment: Callable[..., AbstractContextManager[dict[str, str]]] = (
        provision_disposable_validation_environment
    ),
    ensure_repository_idle: Callable[[Path], None] = require_external_clawpatch_preflight,
    heartbeat_seconds: float = 30,
    cleanup_root: Path | None = None,
) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "cleanup":
        cleanup_parser = argparse.ArgumentParser(
            prog="clawpatch-supervise cleanup",
            description="Inspect or remove only stale supervisor-owned transient artifacts.",
        )
        cleanup_mode = cleanup_parser.add_mutually_exclusive_group(required=True)
        cleanup_mode.add_argument("--dry-run", action="store_true")
        cleanup_mode.add_argument("--apply", action="store_true")
        cleanup_args = cleanup_parser.parse_args(raw_argv[1:])
        try:
            cleanup_report = cleanup_owned_runs(apply=cleanup_args.apply, root=cleanup_root)
        except SafetyError as exc:
            print(f"STOPPED: {exc}")
            return 2
        print(f"ClawPatch Supervise cleanup root: {cleanup_report.root}")
        for entry in cleanup_report.entries:
            print(f"{entry.status}: {entry.path} ({entry.bytes} bytes)")
        print(
            f"COMPLETE: inspected={len(cleanup_report.entries)} "
            f"removed={cleanup_report.removed} removed_bytes={cleanup_report.removed_bytes}"
        )
        return 0
    parser = argparse.ArgumentParser(
        prog="clawpatch-supervise",
        description="Visibly process ClawPatch's live queue one current finding at a time.",
        epilog=(
            "Transient cleanup: clawpatch-supervise cleanup --dry-run | "
            "clawpatch-supervise cleanup --apply"
        ),
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--print-state-path",
        action="store_true",
        help="print this repository's durable standalone state directory and exit",
    )
    parser.add_argument("--branch", default="current")
    parser.add_argument("--push", choices=("none", "each", "final"), default="each")
    parser.add_argument("--publish-clawpatch-state", action="store_true")
    parser.add_argument("--trusted-host-codex-sandbox-bypass", action="store_true")
    start_mode = parser.add_mutually_exclusive_group()
    start_mode.add_argument(
        "--fresh",
        dest="fresh",
        action="store_true",
        default=None,
        help="explicitly remove clean existing ClawPatch state and start a fresh review",
    )
    start_mode.add_argument(
        "--resume-stopped",
        dest="fresh",
        action="store_false",
        help="resume existing state or one exactly checkpoint-owned stopped attempt",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=CLAWPATCH_CHILD_WATCHDOG_SECONDS // 60,
    )
    args = parser.parse_args(raw_argv)
    if args.timeout_minutes < 1:
        parser.error("--timeout-minutes must be at least 1")
    try:
        repo = Path(args.repo).resolve()
    except (OSError, RuntimeError) as exc:
        print(f"STOPPED: Could not resolve repository path {args.repo!r}: {exc}")
        return 2
    if args.print_state_path:
        print(external_state_root(repo))
        return 0
    watchdog_seconds = args.timeout_minutes * 60

    state: dict[str, Any] = {
        "phase": "starting",
        "current": "?",
        "total": "?",
        "finding_id": "",
        "changed": time.monotonic(),
    }
    state_lock = threading.Lock()
    stopped = threading.Event()

    def display(event: dict[str, Any]) -> None:
        with state_lock:
            for key in ("command", "finding_id", "attempt", "max_attempts"):
                state.pop(key, None)
            state.update(event)
            state["changed"] = time.monotonic()
        print(_render_event(event), flush=True)

    def display_after_external_preflight(event: dict[str, Any]) -> None:
        if event.get("phase") != "preflight":
            display(event)

    def heartbeat() -> None:
        while not stopped.wait(heartbeat_seconds):
            with state_lock:
                snapshot = dict(state)
            elapsed = int(time.monotonic() - float(snapshot["changed"]))
            phase = str(snapshot.get("phase", "working"))
            attempt = snapshot.get("attempt")
            maximum = snapshot.get("max_attempts")
            attempt_text = f" attempt {attempt}/{maximum}" if attempt and maximum else ""
            if attempt and not maximum:
                attempt_text = f" attempt {attempt}"
            finding = f" {snapshot['finding_id']}" if snapshot.get("finding_id") else ""
            lines = [
                f"{_counter(snapshot)} still running: {phase}{attempt_text}{finding}",
                f"({elapsed}s in this displayed phase; child watchdog is {watchdog_seconds}s)",
            ]
            if snapshot.get("command"):
                lines.append(f"$ {snapshot['command']}")
            print("\n".join(lines), flush=True)

    thread = None
    if heartbeat_seconds > 0:
        thread = threading.Thread(
            target=heartbeat, name="clawpatch-supervise-heartbeat", daemon=True
        )
        thread.start()

    print("🤬🦶💥 NEW AND FUCKING IMPROVED — NOW WITH MORE CURSING", flush=True)
    print(
        f"ClawPatch external supervisor: repo={repo} "
        f"branch={args.branch} push={args.push} fresh={'auto' if args.fresh is None else args.fresh} "
        f"timeout={args.timeout_minutes}m",
        flush=True,
    )
    try:
        display(
            {
                "phase": "preflight",
                "current": "?",
                "total": "?",
                "command": "clawpatch --version",
                "attempt": 1,
                "max_attempts": 1,
            }
        )
        ensure_repository_idle(repo)
        resolved_fresh = _resolve_fresh_mode(repo, args.fresh)
        with (
            owned_run_directory(repo, root=cleanup_root) as owned_run,
            provision_validation_environment(
                repo,
                progress=display,
                temporary_root=owned_run.temporary_root,
            ) as validation_env_overrides,
        ):
            child_env_overrides = {
                **validation_env_overrides,
                **owned_run.child_environment(),
            }
            report = run_sweep(
                repo,
                apply=True,
                branch=args.branch,
                push_mode=args.push,
                publish_clawpatch_state=args.publish_clawpatch_state,
                trusted_host_codex_sandbox_bypass=args.trusted_host_codex_sandbox_bypass,
                fresh=resolved_fresh,
                child_timeout_seconds=watchdog_seconds,
                progress=display_after_external_preflight,
                integration_mode="external",
                child_env_overrides=child_env_overrides,
            )
    except ClawpatchStop as exc:
        print("\n🛑💥🤬 FUCK. SUPERVISOR STOPPED SAFELY.", flush=True)
        print(f"\nSTOPPED: {exc}", flush=True)
        if exc.repair_action is RepairAction.STOP_TRANSIENT:
            print(
                "TRANSIENT: service managers may resume this exact checkpoint with "
                "--resume-stopped.",
                flush=True,
            )
            return 75
        return 2
    except ClawpatchCommandFailure as exc:
        print("\n💣😤🔧 COMMAND BLEW UP. SOURCE PROOF STILL WINS.", flush=True)
        print(f"\nSTOPPED: {exc}", flush=True)
        if exc.failure.transient:
            print(
                "TRANSIENT: service managers may restart this source-clean command.",
                flush=True,
            )
            return 75
        return 2
    except SafetyError as exc:
        print("\n🛑🧱🤬 SAFETY CHECK CAUGHT SOME SKETCHY SHIT.", flush=True)
        print(f"\nSTOPPED: {exc}", flush=True)
        return 2
    except KeyboardInterrupt:
        print(
            "\n✋💥 INTERRUPTED: applied source or checkpoint changes may remain. "
            "Inspect the repository and ClawPatch state before choosing how to resume.",
            flush=True,
        )
        return 130
    finally:
        stopped.set()
        if thread is not None:
            thread.join(timeout=1)

    if not report.get("ok"):
        print(
            "\nSTOPPED: "
            f"fixed={report.get('finding_count', 0)} "
            f"open={report.get('open_findings', '?')} "
            f"fresh_review_generations={len(report.get('review_generations', []))} "
            f"head={report.get('git_head', '')} "
            "— 🛑💥🤬 SWEEP FAILED. QUEUE ISN'T CLEAN.",
            flush=True,
        )
        return 2

    print(
        "\nCOMPLETE: "
        f"fixed={report.get('finding_count', 0)} "
        f"open={report.get('open_findings', '?')} "
        f"fresh_review_generations={len(report.get('review_generations', []))} "
        f"head={report.get('git_head', '')} "
        "— 🏁🔥🤘 FUCK YES. QUEUE'S CLEAN.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
