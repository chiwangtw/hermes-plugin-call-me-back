#!/bin/bash
# Register the Codex adapter globally: add hook.sh to $CODEX_HOME/hooks.json (default ~/.codex) for
# Stop, PermissionRequest and SessionEnd, keeping every existing hook. Idempotent; backs the file up first.
#
#   install.sh [<installed adapter dir>] [<backup stamp>]      # machine/install.sh --codex calls it this way
#   CODEX_HOME=<other home> install.sh …                       # e.g. Orca's codex-runtime-home/home
#
# Codex runs new or changed hooks only after they are trusted: start `codex` once interactively and
# accept "Hooks need review" (or review them under /hooks).
set -euo pipefail

dir="${1:-$HOME/.local/share/call-me-back/adapters/codex}"
stamp="${2:-$(date +%Y%m%d%H%M%S)}"
home="${CODEX_HOME:-$HOME/.codex}"
file="$home/hooks.json"
# Codex runs hook commands through a shell; quote the path in case it contains spaces.
cmd="'$dir/hook.sh'"

command -v jq >/dev/null || { echo "codex: jq is required" >&2; exit 1; }
[ -x "$dir/hook.sh" ] || { echo "codex: $dir/hook.sh is missing or not executable" >&2; exit 1; }

mkdir -p "$home"
if [ -f "$file" ]; then
  cp -p "$file" "$file.bak-call-me-back-$stamp" && echo "  backup: $file.bak-call-me-back-$stamp"
else
  echo '{"hooks":{}}' > "$file"
fi

jq --arg cmd "$cmd" '
  def ensure($event; $timeout):
    .hooks[$event] = ((.hooks[$event] // [])
      | if any(.[]; any(.hooks[]?; .command == $cmd)) then .
        else . + [{hooks: [{type: "command", command: $cmd, timeout: $timeout}]}] end);
  .hooks = (.hooks // {})
  | ensure("Stop"; 10) | ensure("PermissionRequest"; 10) | ensure("SessionEnd"; 3)' "$file" > "$file.tmp"
mv "$file.tmp" "$file"
echo "codex: hooks in $file (start codex once and trust the new hooks: /hooks)"
