#!/bin/bash
# Install the call-me-back execution-machine side (macOS / Linux).
#
#   machine/install.sh [--receive "ssh hermes-host hermes call-me-back receive"] [--claude-code] [--codex] [--pi]
#
# Copies scripts to ~/.local/share/call-me-back, links call-me-back-{emit,run} into ~/.local/bin,
# writes ~/.config/call-me-back/machine.env, and registers the chosen executor adapters globally.
# Every settings file it edits is backed up next to itself as <file>.bak-call-me-back-<timestamp>.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
SHARE="$HOME/.local/share/call-me-back"
BIN="$HOME/.local/bin"
CONF_DIR="$HOME/.config/call-me-back"
STAMP=$(date +%Y%m%d%H%M%S)

receive="" claude=0 codex=0 pi=0
while [ $# -gt 0 ]; do
  case "$1" in
    --receive) receive="$2"; shift 2 ;;
    --claude-code) claude=1; shift ;;
    --codex) codex=1; shift ;;
    --pi) pi=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

backup() { [ -f "$1" ] && cp -p "$1" "$1.bak-call-me-back-$STAMP" && echo "  backup: $1.bak-call-me-back-$STAMP"; return 0; }

echo "scripts -> $SHARE"
mkdir -p "$SHARE" "$BIN"
rm -rf "$SHARE/bin" "$SHARE/adapters"
cp -R "$SRC/bin" "$SRC/adapters" "$SHARE/"
chmod +x "$SHARE"/bin/* "$SHARE"/adapters/*/*.sh 2>/dev/null || true
ln -sf "$SHARE/bin/call-me-back-emit" "$BIN/call-me-back-emit"
ln -sf "$SHARE/bin/call-me-back-run" "$BIN/call-me-back-run"

mkdir -p "$CONF_DIR"
if [ -n "$receive" ]; then
  backup "$CONF_DIR/machine.env"
  printf '# call-me-back execution-machine settings (sourced by call-me-back-emit / call-me-back-run)\nCALL_ME_BACK_RECEIVE=%q\n' \
    "$receive" > "$CONF_DIR/machine.env"
  echo "receiver: $receive"
elif [ ! -f "$CONF_DIR/machine.env" ]; then
  echo "note: no --receive given; defaulting to 'hermes call-me-back receive' on this machine"
fi

if [ "$claude" = 1 ]; then
  settings="$HOME/.claude/settings.json"
  hook="$SHARE/adapters/claude-code/hook.sh"
  echo "claude-code: hooks in $settings"
  mkdir -p "$(dirname "$settings")"; [ -f "$settings" ] || echo '{}' > "$settings"
  backup "$settings"
  jq --arg hook "$hook" '
    def ensure($event):
      .hooks[$event] = ((.hooks[$event] // [])
        | if any(.[]; any(.hooks[]?; .command == $hook)) then . else . + [{hooks: [{type: "command", command: $hook}]}] end);
    ensure("Stop") | ensure("Notification") | ensure("SessionEnd")' "$settings" > "$settings.tmp"
  mv "$settings.tmp" "$settings"
fi

if [ "$codex" = 1 ]; then
  if [ -x "$SRC/adapters/codex/install.sh" ]; then "$SRC/adapters/codex/install.sh" "$SHARE/adapters/codex" "$STAMP"
  else echo "codex: adapter installer missing" >&2; fi
fi

if [ "$pi" = 1 ]; then
  if [ -x "$SRC/adapters/pi/install.sh" ]; then "$SRC/adapters/pi/install.sh" "$SHARE/adapters/pi" "$STAMP"
  else echo "pi: adapter installer missing" >&2; fi
fi

echo "done. test: printf '{}' | $BIN/call-me-back-emit --executor claude-code --reason turn_end  (untracked: no-op)"
