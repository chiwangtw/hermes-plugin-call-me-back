#!/bin/bash
# Register the pi adapter globally: add call-me-back.ts to "extensions" in $PI_CODING_AGENT_DIR/settings.json
# (default ~/.pi/agent). Idempotent; backs the file up first. Takes effect for pi sessions started afterwards.
#
#   install.sh [<installed adapter dir>] [<backup stamp>]      # machine/install.sh --pi calls it this way
set -euo pipefail

dir="${1:-$HOME/.local/share/call-me-back/adapters/pi}"
stamp="${2:-$(date +%Y%m%d%H%M%S)}"
agent_dir="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
file="$agent_dir/settings.json"
ext="$dir/call-me-back.ts"

command -v jq >/dev/null || { echo "pi: jq is required" >&2; exit 1; }
[ -f "$ext" ] || { echo "pi: $ext is missing" >&2; exit 1; }

mkdir -p "$agent_dir"
if [ -f "$file" ]; then
  cp -p "$file" "$file.bak-call-me-back-$stamp" && echo "  backup: $file.bak-call-me-back-$stamp"
else
  echo '{}' > "$file"
fi

jq --arg ext "$ext" '.extensions = ((.extensions // []) | if index($ext) then . else . + [$ext] end)' \
  "$file" > "$file.tmp"
mv "$file.tmp" "$file"
echo "pi: extension registered in $file"
