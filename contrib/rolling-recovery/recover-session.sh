#!/usr/bin/env bash
# Inspect and selectively restore rolling i3/Kitty snapshots.

set -euo pipefail

state_root="${I3_RECOVERY_STATE_ROOT:-$HOME/.local/state/i3-rolling-recovery}"
snapshot_root="$state_root/snapshots"
mkdir -p "$snapshot_root"

usage() {
    cat <<'EOF'
Usage:
  recover-session.sh list [count]
  recover-session.sh show [latest|YYYYMMDD-HHMMSS]
  recover-session.sh kitty <workspace-number> [snapshot] [kitty-index]
  recover-session.sh workspace <workspace-number> [snapshot]

Examples:
  recover-session.sh list
  recover-session.sh show latest
  recover-session.sh kitty 1 latest
  recover-session.sh workspace 7 20260903-221000

The kitty command opens a safe copy of the saved Kitty OS window, including its
original tabs and pane layout. Foreground commands such as Pi are recorded in
*.state.json for inspection but are not restarted automatically.
EOF
}

resolve_snapshot() {
    local requested="${1:-latest}"
    local resolved
    if [[ "$requested" == latest ]]; then
        resolved="$(find "$snapshot_root" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f\n' 2>/dev/null | sort -r | sed -n '1p')"
        [[ -n "$resolved" ]] || { echo "No rolling snapshots found." >&2; return 1; }
    else
        resolved="${requested##*/}"
    fi
    [[ "$resolved" =~ ^[0-9]{8}-[0-9]{6}$ ]] || {
        echo "Invalid snapshot name: $requested" >&2
        return 1
    }
    local path="$snapshot_root/$resolved"
    [[ -f "$path/metadata.json" ]] || {
        echo "Snapshot not found: $path" >&2
        return 1
    }
    printf '%s\n' "$path"
}

list_snapshots() {
    local count="${1:-20}"
    [[ "$count" =~ ^[0-9]+$ ]] || { echo "Count must be numeric." >&2; return 2; }
    find "$snapshot_root" -mindepth 1 -maxdepth 1 -type d ! -name '.*' -printf '%f\n' 2>/dev/null \
        | sort -r | sed -n "1,${count}p" | while read -r name; do
            local metadata="$snapshot_root/$name/metadata.json"
            [[ -f "$metadata" ]] || continue
            jq -r '[.saved_at, (.workspaces | length), (.warnings | length)] | @tsv' "$metadata" \
                | awk -v name="$name" -F '\t' '{printf "%s  %s  workspaces=%s warnings=%s\n", name, $1, $2, $3}'
        done
}

show_snapshot() {
    local path
    path="$(resolve_snapshot "${1:-latest}")"
    python3 - "$path" <<'PY'
import json
from pathlib import Path
import sys

root = Path(sys.argv[1])
metadata = json.loads((root / "metadata.json").read_text())
print(f"Snapshot: {root.name}")
print(f"Saved:    {metadata.get('saved_at')}")
print(f"Scope:    {metadata.get('workspace_scope')}")
for workspace in metadata.get("workspaces", []):
    print(f"  WS {workspace.get('num')}: {workspace.get('name')} [{workspace.get('output')}]")
for state_file in sorted((root / "kitty").glob("*.state.json")):
    print(f"\nKitty: {state_file.stem.removesuffix('.state')}")
    for os_window in json.loads(state_file.read_text()):
        for tab_index, tab in enumerate(os_window.get("tabs", []), 1):
            print(f"  Tab {tab_index}: {tab.get('title')} ({tab.get('layout')})")
            for pane_index, pane in enumerate(tab.get("windows", []), 1):
                processes = pane.get("foreground_processes") or []
                command = " ".join(map(str, processes[-1].get("cmdline", []))) if processes else "shell"
                print(f"    Pane {pane_index}: {pane.get('cwd')} :: {command}")
for warning in metadata.get("warnings", []):
    print(f"WARNING: {warning}")
PY
}

restore_kitty() {
    local workspace="$1"
    local path
    path="$(resolve_snapshot "${2:-latest}")"
    local index="${3:-1}"
    [[ "$index" =~ ^[0-9]+$ && "$index" -ge 1 ]] || {
        echo "Kitty index must be a positive number." >&2
        return 2
    }
    mapfile -t sessions < <(find "$path/kitty" -maxdepth 1 -type f \
        \( -name "workspace-${workspace}.kitty-session" -o -name "workspace-${workspace}-*.kitty-session" \) \
        | sort)
    if (( ${#sessions[@]} == 0 )); then
        echo "No Kitty session for workspace $workspace in ${path##*/}." >&2
        return 1
    fi
    if (( index > ${#sessions[@]} )); then
        echo "Workspace $workspace has only ${#sessions[@]} saved Kitty window(s)." >&2
        return 1
    fi
    local session="${sessions[index-1]}"
    echo "Opening safe Kitty recovery copy: $session"
    nohup kitty --session "$session" >"$HOME/.cache/i3-kitty-recovery.log" 2>&1 &
}

restore_workspace() {
    local workspace="$1"
    local path
    path="$(resolve_snapshot "${2:-latest}")"
    [[ "$workspace" =~ ^[0-9]+$ ]] || { echo "Workspace must be numeric." >&2; return 2; }
    local layout="$path/workspace_${workspace}_layout.json"
    [[ -f "$layout" ]] || { echo "No saved workspace $workspace in ${path##*/}." >&2; return 1; }
    printf 'Restore workspace %s from %s? This can duplicate surviving windows. [y/N] ' "$workspace" "${path##*/}"
    read -r answer
    [[ "$answer" =~ ^[Yy]$ ]] || { echo "Cancelled."; return 0; }
    i3-resurrect restore --workspace "$workspace" --numeric --directory "$path"
    local output
    output="$(jq -r --argjson ws "$workspace" '.workspaces[] | select(.num == $ws) | .output // empty' "$path/metadata.json")"
    if [[ -n "$output" ]] && xrandr --query | grep -q "^${output} connected"; then
        i3-msg "workspace number $workspace; move workspace to output \"$output\"" >/dev/null
    fi
}

command="${1:-list}"
case "$command" in
    list) list_snapshots "${2:-20}" ;;
    show) show_snapshot "${2:-latest}" ;;
    kitty)
        [[ $# -ge 2 ]] || { usage >&2; exit 2; }
        restore_kitty "$2" "${3:-latest}" "${4:-1}"
        ;;
    workspace)
        [[ $# -ge 2 ]] || { usage >&2; exit 2; }
        restore_workspace "$2" "${3:-latest}"
        ;;
    -h|--help|help) usage ;;
    *) usage >&2; exit 2 ;;
esac
