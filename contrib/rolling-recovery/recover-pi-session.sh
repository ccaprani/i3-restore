#!/usr/bin/env bash
# Find Pi sessions associated with panes in a rolling i3/Kitty snapshot.

set -euo pipefail

state_root="${I3_RECOVERY_STATE_ROOT:-$HOME/.local/state/i3-rolling-recovery}"
snapshot_root="$state_root/snapshots"
pi_session_root="${PI_SESSION_ROOT:-$HOME/.pi/agent/sessions}"

usage() {
    cat <<'EOF'
Usage:
  recover-pi-session.sh list [latest|snapshot]
  recover-pi-session.sh launch <pane-number> [latest|snapshot]

Lists the latest saved Pi session associated with every Pi pane in a Kitty
snapshot. `launch` opens that session in a new Kitty OS window.
EOF
}

resolve_snapshot() {
    local requested="${1:-latest}"
    local name="$requested"
    if [[ "$requested" == latest ]]; then
        name="$(find "$snapshot_root" -mindepth 1 -maxdepth 1 -type d ! -name '.*' \
            -printf '%f\n' 2>/dev/null | sort -r | sed -n '1p')"
    fi
    local path="$snapshot_root/$name"
    [[ -n "$name" && -f "$path/metadata.json" ]] || {
        echo "Snapshot not found: $requested" >&2
        return 1
    }
    printf '%s\n' "$path"
}

get_pi_sessions() {
    local snapshot="$1"
    python3 - "$snapshot" "$pi_session_root" <<'PY'
import json
from pathlib import Path
import sys

snapshot, session_root = map(Path, sys.argv[1:])
seen = set()
for state_file in sorted((snapshot / "kitty").glob("*.state.json")):
    for os_window in json.loads(state_file.read_text()):
        for tab in os_window.get("tabs", []):
            for pane in tab.get("windows", []):
                commands = [
                    process.get("cmdline", [])
                    for process in pane.get("foreground_processes", [])
                ]
                is_pi = any(
                    command and Path(str(command[0])).name == "pi" for command in commands
                )
                cwd = pane.get("cwd")
                if not is_pi or not cwd or cwd in seen:
                    continue
                seen.add(cwd)
                session_dir = session_root / f"--{cwd.strip('/').replace('/', '-')}--"
                sessions = sorted(
                    session_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True
                )
                session = str(sessions[0]) if sessions else ""
                print(f"{cwd}\t{session}")
PY
}

command="${1:-list}"
case "$command" in
    list)
        snapshot="$(resolve_snapshot "${2:-latest}")"
        index=0
        while IFS=$'\t' read -r cwd session; do
            ((index += 1))
            printf '%s\t%s\t%s\n' "$index" "$cwd" "${session:-<no saved Pi session>}"
        done < <(get_pi_sessions "$snapshot")
        ;;
    launch)
        [[ $# -ge 2 && "$2" =~ ^[1-9][0-9]*$ ]] || { usage >&2; exit 2; }
        snapshot="$(resolve_snapshot "${3:-latest}")"
        mapfile -t sessions < <(get_pi_sessions "$snapshot")
        entry="${sessions[$((10#$2 - 1))]:-}"
        [[ -n "$entry" ]] || { echo "No Pi pane numbered $2." >&2; exit 1; }
        cwd="${entry%%$'\t'*}"
        session="${entry#*$'\t'}"
        [[ -f "$session" ]] || { echo "No saved Pi session for $cwd." >&2; exit 1; }
        command -v pi >/dev/null || { echo "pi is not on PATH." >&2; exit 1; }
        exec kitty --directory "$cwd" pi --session "$session"
        ;;
    -h|--help|help) usage ;;
    *) usage >&2; exit 2 ;;
esac
