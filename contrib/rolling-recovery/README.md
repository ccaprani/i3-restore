# Rolling recovery toolkit

Optional personal tooling for keeping timestamped i3 and Kitty recovery points. It is deliberately
kept under `contrib/`: it supplements i3-restore rather than changing its default, last-session
workflow.

Each snapshot records every numbered workspace (and a non-empty scratchpad), its i3-resurrect
layout/program state, plus a Kitty-native session file for every Kitty OS window. Kitty state
manifests expose tabs, panes, layouts, working directories and foreground commands to support
selective recovery. Credential-like command arguments are redacted before persistence, and unsafe
foreground commands are removed from restored Kitty session files.

## Dependencies

- i3, i3-msg and i3-resurrect
- Kitty with a usable remote-control socket (`kitty @ ls` must work for each window)
- jq, xprop, xrandr, Python 3 and systemd user services

## Install

```bash
install -d ~/.local/lib/i3-rolling-recovery ~/.config/systemd/user ~/.local/bin
install -m 755 contrib/rolling-recovery/{save-session.py,snapshot-session.sh,recover-session.sh} \
  ~/.local/lib/i3-rolling-recovery/
install -m 644 contrib/rolling-recovery/systemd/* ~/.config/systemd/user/
ln -sfn ~/.local/lib/i3-rolling-recovery/recover-session.sh ~/.local/bin/i3-session-recover
systemctl --user daemon-reload
systemctl --user enable --now i3-rolling-recovery.timer
```

By default, snapshots are stored in `~/.local/state/i3-rolling-recovery/snapshots/`. The timer
keeps 1008 generations, equivalent to seven days at ten-minute intervals. Override either setting:

```bash
mkdir -p ~/.config/systemd/user/i3-rolling-recovery.service.d
cat >~/.config/systemd/user/i3-rolling-recovery.service.d/retention.conf <<'EOF'
[Service]
Environment=I3_RECOVERY_KEEP_SNAPSHOTS=288
Environment=I3_RECOVERY_STATE_ROOT=%h/.local/state/i3-rolling-recovery
EOF
systemctl --user daemon-reload
systemctl --user restart i3-rolling-recovery.timer
```

## Recovery

```bash
i3-session-recover list
i3-session-recover show latest
i3-session-recover kitty 1 latest
i3-session-recover workspace 7 20260903-221000
```

`show` lets you identify the target Kitty tab/pane by working directory and foreground process.
`kitty` opens a safe copy of its saved tab and pane layout; it does not automatically rerun Pi,
SSH, or other unsafe foreground processes. A Pi conversation can then be resumed using `pi -r` or
`pi --session <path>`. `workspace` asks for confirmation because it may duplicate applications
that are still running.
