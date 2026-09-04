#!/usr/bin/env bash
# Create one all-workspace rolling i3/Kitty snapshot when i3 is available.

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
keep_snapshots="${I3_RECOVERY_KEEP_SNAPSHOTS:-1008}"

socket="$(env -u I3SOCK i3 --get-socketpath 2>/dev/null || true)"
if [[ -z "$socket" || ! -S "$socket" ]]; then
    exit 0
fi

export I3SOCK="$socket"
exec "$script_dir/save-session.py" \
    --archive \
    --all-workspaces \
    --keep "$keep_snapshots" \
    --wait-lock
