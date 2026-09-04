#!/usr/bin/env python3
"""Save live i3 workspaces and Kitty layouts as recoverable snapshots."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

STATE_ROOT = Path(
    os.environ.get(
        "I3_RECOVERY_STATE_ROOT", Path.home() / ".local/state/i3-rolling-recovery"
    )
)
SESSIONS_ROOT = STATE_ROOT / "sessions"
SNAPSHOTS_ROOT = STATE_ROOT / "snapshots"
LOCK_FILE = Path(
    os.environ.get(
        "I3_RECOVERY_LOCK_FILE",
        Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "i3-rolling-recovery.lock",
    )
)
BLACKLIST = '/\\:*"<>|'
DEFAULT_WORKSPACE_NUMBERS = frozenset(range(1, 7))
SAFE_KITTY_COMMANDS = frozenset({"btop", "cmatrix", "nvim", "nvtop", "vim"})
SENSITIVE_ARGUMENT_NAMES = frozenset(
    {
        "--api-key",
        "--password",
        "--secret",
        "--token",
        "-p",
    }
)
SENSITIVE_ARGUMENT_PREFIXES = (
    "sk-",
    "ghp_",
    "github_pat_",
    "glpat-",
    "xoxb-",
    "xoxp-",
)


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def filename_filter(name: str) -> str:
    for char in BLACKLIST:
        name = name.replace(char, "")
    return name


def get_tree() -> dict:
    return json.loads(run(["i3-msg", "-t", "get_tree"]).stdout)


def get_workspaces() -> list[dict]:
    return json.loads(run(["i3-msg", "-t", "get_workspaces"]).stdout)


def window_pid(window_id: int) -> int | None:
    result = run(["xprop", "_NET_WM_PID", "-id", str(window_id)], check=False)
    try:
        return int(result.stdout.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return None


def workspace_windows(node: dict, current: str | None = None):
    if node.get("type") == "workspace":
        current = node.get("name")
    if current is not None and node.get("window") is not None:
        yield current, node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        yield from workspace_windows(child, current)


def kitty_command_is_safe(command: str | list[str]) -> bool:
    """Allow only explicitly safe foreground commands to restart after login."""
    argv = shlex.split(command) if isinstance(command, str) else command
    if not argv:
        return False
    executable = Path(argv[0]).name
    if executable in SAFE_KITTY_COMMANDS or executable.startswith("nvtop-"):
        return True
    if executable == "tty-worldclock":
        return True
    # The local `llm live` subcommand is a read-only RAM/VRAM dashboard.
    return executable == "llm" and len(argv) > 1 and argv[1] == "live"


def sanitize_kitty_session(source: Path, destination: Path) -> None:
    """Strip unsafe foreground commands while retaining Kitty's full layout."""
    sanitized: list[str] = []
    prefix = "kitty-unserialize-data="
    for line in source.read_text().splitlines():
        if not line.startswith("launch "):
            sanitized.append(line)
            continue
        tokens = shlex.split(line)
        changed = False
        for index, token in enumerate(tokens):
            if not token.startswith(prefix):
                continue
            data = json.loads(token[len(prefix) :])
            embedded_command = data.get("cmd_at_shell_startup")
            trailing_command = tokens[index + 1 :]
            if embedded_command and not kitty_command_is_safe(embedded_command):
                del data["cmd_at_shell_startup"]
                tokens[index] = prefix + json.dumps(data, separators=(",", ":"))
                changed = True
            if trailing_command and not kitty_command_is_safe(trailing_command):
                del tokens[index + 1 :]
                changed = True
            break
        sanitized.append(shlex.join(tokens) if changed else line)
    destination.write_text("\n".join(sanitized) + "\n")


def redact_command(command: object) -> object:
    """Remove likely credentials from a command-line array before persistence."""
    if not isinstance(command, list):
        return command
    redacted: list[object] = []
    redact_next = False
    for argument in command:
        if not isinstance(argument, str):
            redacted.append(argument)
            continue
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        option, separator, value = argument.partition("=")
        if option in SENSITIVE_ARGUMENT_NAMES:
            if separator:
                redacted.append(f"{option}=<redacted>")
            else:
                redacted.append(argument)
                redact_next = True
            continue
        if argument.startswith(SENSITIVE_ARGUMENT_PREFIXES):
            redacted.append("<redacted>")
            continue
        redacted.append(argument)
    return redacted


def compact_kitty_state(raw_state: list[dict]) -> list[dict]:
    """Keep recovery-relevant Kitty topology without copying window env data."""
    state: list[dict] = []
    for os_window in raw_state:
        saved_os_window = {
            "id": os_window.get("id"),
            "platform_window_id": os_window.get("platform_window_id"),
            "wm_class": os_window.get("wm_class"),
            "tabs": [],
        }
        for tab in os_window.get("tabs", []):
            saved_tab = {
                "id": tab.get("id"),
                "title": tab.get("title"),
                "is_active": tab.get("is_active"),
                "layout": tab.get("layout"),
                "layout_opts": tab.get("layout_opts"),
                "layout_state": tab.get("layout_state"),
                "windows": [],
            }
            for window in tab.get("windows", []):
                saved_tab["windows"].append(
                    {
                        "id": window.get("id"),
                        "title": window.get("title"),
                        "cwd": window.get("cwd"),
                        "cmdline": redact_command(window.get("cmdline")),
                        "foreground_processes": [
                            {
                                "pid": process.get("pid"),
                                "cwd": process.get("cwd"),
                                "cmdline": redact_command(process.get("cmdline")),
                            }
                            for process in window.get("foreground_processes", [])
                        ],
                        "is_active": window.get("is_active"),
                        "at_prompt": window.get("at_prompt"),
                    }
                )
            saved_os_window["tabs"].append(saved_tab)
        state.append(saved_os_window)
    return state


def save_kitty_session(pid: int, destination: Path) -> None:
    socket = Path(f"/tmp/kitty-{pid}")
    if not socket.exists():
        raise RuntimeError(f"Kitty remote-control socket is missing: {socket}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    target = f"unix:{socket}"
    state_result = run(["kitty", "@", "--to", target, "ls"], check=False)
    if state_result.returncode:
        raise RuntimeError(state_result.stderr.strip() or f"Could not inspect Kitty PID {pid}")
    state_file = destination.with_suffix(".state.json")
    state_file.write_text(
        json.dumps(compact_kitty_state(json.loads(state_result.stdout)), indent=2) + "\n"
    )

    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    raw_session = runtime_dir / f"i3-kitty-session-{pid}-{time.time_ns()}.raw"
    try:
        result = run(
            [
                "kitty",
                "@",
                "--to",
                target,
                "action",
                "save_as_session",
                "--save-only",
                "--use-foreground-process",
                str(raw_session),
            ],
            check=False,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"Could not save Kitty PID {pid}")
        for _ in range(30):
            if raw_session.is_file() and raw_session.stat().st_size:
                sanitize_kitty_session(raw_session, destination)
                return
            time.sleep(0.1)
        raise RuntimeError(f"Kitty did not create {raw_session}")
    finally:
        raw_session.unlink(missing_ok=True)


def patch_program_commands(staging_dir: Path) -> None:
    """Replace captured implementation paths with stable launch wrappers."""
    zathura_wrapper = Path.home() / ".local/bin/zathura"
    for programs_file in staging_dir.glob("workspace_*_programs.json"):
        programs = json.loads(programs_file.read_text())
        changed = False
        for entry in programs:
            command = entry.get("command")
            if (
                isinstance(command, list)
                and command
                and Path(command[0]).name == "zathura"
                and zathura_wrapper.is_file()
            ):
                command[0] = str(zathura_wrapper)
                changed = True
        if changed:
            programs_file.write_text(json.dumps(programs, indent=2) + "\n")


def patch_kitty_programs(
    tree: dict,
    workspace_keys: dict[str, str],
    staging_dir: Path,
    final_dir: Path,
) -> list[str]:
    warnings: list[str] = []
    kitty_by_workspace: dict[str, list[dict]] = {}
    for workspace, window in workspace_windows(tree):
        props = window.get("window_properties") or {}
        if workspace in workspace_keys and props.get("class") == "kitty":
            kitty_by_workspace.setdefault(workspace, []).append(window)

    for workspace, windows in kitty_by_workspace.items():
        workspace_key = workspace_keys[workspace]
        programs_file = staging_dir / f"workspace_{workspace_key}_programs.json"
        if not programs_file.is_file():
            warnings.append(f"No programs file for Kitty workspace {workspace!r}")
            continue
        programs = json.loads(programs_file.read_text())
        kitty_entries = [
            item
            for item in programs
            if item.get("command")
            and Path(item["command"][0]).name == "kitty"
        ]
        if len(kitty_entries) != len(windows):
            warnings.append(
                f"Kitty count mismatch on {workspace!r}: "
                f"{len(windows)} windows, {len(kitty_entries)} saved commands"
            )

        for index, (window, entry) in enumerate(zip(windows, kitty_entries), start=1):
            pid = window_pid(window["window"])
            if pid is None:
                warnings.append(f"Could not find PID for Kitty on {workspace!r}")
                continue
            basename = f"workspace-{workspace_key}"
            if len(windows) > 1:
                basename += f"-{index}"
            staged_session = staging_dir / "kitty" / f"{basename}.kitty-session"
            final_session = final_dir / "kitty" / staged_session.name
            try:
                save_kitty_session(pid, staged_session)
            except (RuntimeError, json.JSONDecodeError) as error:
                warnings.append(str(error))
                continue
            entry["command"] = ["kitty", "--session", str(final_session)]
            entry["working_directory"] = str(Path.home())

        programs_file.write_text(json.dumps(programs, indent=2) + "\n")

    return warnings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        action="store_true",
        help="create a timestamped snapshot instead of rotating reboot state",
    )
    parser.add_argument(
        "--all-workspaces",
        action="store_true",
        help="save every numbered workspace plus the scratchpad",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=1008,
        metavar="COUNT",
        help="number of archived snapshots to retain (default: 1008, seven days at 10 minutes)",
    )
    parser.add_argument(
        "--wait-lock",
        action="store_true",
        help="wait for another snapshot operation instead of failing immediately",
    )
    return parser.parse_args()


def scratchpad_has_windows(tree: dict) -> bool:
    return any(
        workspace == "__i3_scratch" and window.get("window") is not None
        for workspace, window in workspace_windows(tree)
    )


def prune_snapshots(keep: int) -> None:
    snapshots = sorted(
        path
        for path in SNAPSHOTS_ROOT.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    for old_snapshot in snapshots[: max(0, len(snapshots) - keep)]:
        shutil.rmtree(old_snapshot)


def main() -> int:
    args = parse_args()
    if args.keep < 1:
        print("--keep must be at least 1", file=sys.stderr)
        return 2

    if args.archive:
        snapshot_name = time.strftime("%Y%m%d-%H%M%S")
        final_dir = SNAPSHOTS_ROOT / snapshot_name
        staging_dir = SNAPSHOTS_ROOT / f".{snapshot_name}.new"
        previous_dir = None
    else:
        final_dir = SESSIONS_ROOT / "reboot"
        staging_dir = SESSIONS_ROOT / "reboot.new"
        previous_dir = SESSIONS_ROOT / "reboot.previous"

    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as lock:
        lock_flags = fcntl.LOCK_EX if args.wait_lock else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(lock, lock_flags)
        except BlockingIOError:
            print("An i3 session save is already running.", file=sys.stderr)
            return 1

        if not os.environ.get("I3SOCK"):
            print("I3SOCK is not set; cannot save the live i3 session.", file=sys.stderr)
            return 1

        final_dir.parent.mkdir(parents=True, exist_ok=True)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

        tree = get_tree()
        workspaces = [
            workspace
            for workspace in get_workspaces()
            if workspace.get("num", -1) >= 0
            and (
                args.all_workspaces
                or workspace.get("num") in DEFAULT_WORKSPACE_NUMBERS
            )
        ]
        by_number: dict[int, list[str]] = {}
        for workspace in workspaces:
            number = workspace.get("num", -1)
            if number >= 0:
                by_number.setdefault(number, []).append(workspace["name"])
        duplicate_numbers = {
            number: names for number, names in by_number.items() if len(names) > 1
        }
        if duplicate_numbers:
            details = "; ".join(
                f"{number}: {', '.join(names)}"
                for number, names in sorted(duplicate_numbers.items())
            )
            print(
                "Refusing to save while duplicate workspace numbers exist "
                f"({details}).",
                file=sys.stderr,
            )
            return 1

        staging_dir.mkdir(parents=True, exist_ok=True)
        workspace_keys = {ws["name"]: str(ws["num"]) for ws in workspaces}
        for workspace in workspaces:
            run(
                [
                    "i3-resurrect",
                    "save",
                    "--workspace",
                    str(workspace["num"]),
                    "--numeric",
                    "--directory",
                    str(staging_dir),
                    "--swallow=class,instance",
                ]
            )

        include_scratchpad = args.all_workspaces and scratchpad_has_windows(tree)
        if include_scratchpad:
            run(
                [
                    "i3-resurrect",
                    "save",
                    "--workspace",
                    "__i3_scratch",
                    "--directory",
                    str(staging_dir),
                    "--swallow=class,instance",
                ]
            )
            workspace_keys["__i3_scratch"] = "__i3_scratch"

        patch_program_commands(staging_dir)
        warnings = patch_kitty_programs(
            tree, workspace_keys, staging_dir, final_dir
        )
        focused = next((ws for ws in workspaces if ws.get("focused")), None)
        if focused is None and workspaces:
            focused = min(workspaces, key=lambda ws: ws["num"])
        metadata = {
            "format_version": 4,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "workspace_scope": "all" if args.all_workspaces else "1-6",
            "includes_scratchpad": include_scratchpad,
            "focused_workspace": focused,
            "workspaces": workspaces,
            "warnings": warnings,
        }
        (staging_dir / "metadata.json").write_text(
            json.dumps(metadata, indent=2) + "\n"
        )

        for path in staging_dir.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        staging_dir.chmod(0o700)

        if previous_dir is not None:
            if previous_dir.exists():
                shutil.rmtree(previous_dir)
            if final_dir.exists():
                final_dir.rename(previous_dir)
                # Keep the rollback snapshot self-contained after rotation.
                for programs_file in previous_dir.glob("workspace_*_programs.json"):
                    contents = programs_file.read_text()
                    programs_file.write_text(
                        contents.replace(str(final_dir), str(previous_dir))
                    )
        elif final_dir.exists():
            print(f"Snapshot already exists: {final_dir}", file=sys.stderr)
            shutil.rmtree(staging_dir)
            return 1

        staging_dir.rename(final_dir)
        if args.archive:
            prune_snapshots(args.keep)

        kind = "archived snapshot" if args.archive else "reboot session"
        print(f"Saved i3 {kind} to {final_dir}")
        for warning in warnings:
            print(f"WARNING: {warning}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
