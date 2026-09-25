#!/bin/bash
# Codex adapter for call-me-back: Stop / PermissionRequest / SessionEnd hook -> call-me-back-emit.
# Install as a global command hook for those three events (see hooks.json). Prints nothing on stdout
# (Codex parses hook stdout as JSON output), always exits 0; untracked sessions cost one jq call.
# Contract: machine/CONTRACT.md. Verified facts about Codex 0.154 hooks: NOTES.md next to this file.

EMIT="${CALL_ME_BACK_EMIT:-}"
[ -n "$EMIT" ] || EMIT=$(command -v call-me-back-emit 2>/dev/null)
[ -n "$EMIT" ] || EMIT="$HOME/.local/bin/call-me-back-emit"
[ -x "$EMIT" ] || { cat >/dev/null; exit 0; }

input=$(cat)
# \x1f as separator: with a tab, read would collapse empty fields and shift the rest.
IFS=$'\x1f' read -r event session cwd active end_reason tool detail <<EOF
$(printf '%s' "$input" | jq -r '[.hook_event_name // "", .session_id // "", .cwd // "",
    (.stop_hook_active // false | tostring), .reason // "", .tool_name // "",
    (.tool_input | if type == "object" then (.command // .description // tojson)
                   elif . == null then "" else tostring end)]
    | map(tostring | gsub("[\u001f\r\n\t]"; " ")) | join("\u001f")' 2>/dev/null)
EOF

# Fast path: nothing to do unless this Session is (or was) bound to a Task.
if [ -z "${CALL_ME_BACK_TASK-}" ]; then
  state="${CALL_ME_BACK_STATE_DIR:-$HOME/.local/state/call-me-back}"
  safe_session=$(printf '%s' "$session" | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-80)
  [ -n "$session" ] && [ -f "$state/bindings/codex/$safe_session" ] || exit 0
fi

# session_id is the Codex thread id: the same UUID `codex resume <id>` takes, unchanged across resumes.
emit() { "$EMIT" --executor codex --agent-session "$session" --cwd "$cwd" "$@" >/dev/null 2>&1; }

case "$event" in
  Stop)
    [ "$active" = "true" ] && exit 0
    printf '%s' "$input" | jq -r '.last_assistant_message // ""' 2>/dev/null \
      | emit --reason turn_end --message-stdin
    ;;
  PermissionRequest)
    # Fires just before Codex shows its approval prompt; our empty stdout leaves the decision to the human.
    d="${tool:-tool}"
    [ -n "$detail" ] && d="$d: $detail"
    emit --reason needs_approval --detail "$(printf '%s' "$d" | cut -c1-200)"
    ;;
  SessionEnd)
    # The dispatch wrapper reports exits of the Sessions it started (crashes included).
    [ -n "${CALL_ME_BACK_WRAPPED-}" ] && exit 0
    # Codex 0.154 fires no SessionEnd on /clear; it fires one per thread loaded in the process when the
    # process exits (the pre-/clear thread too), reason always "other". One process exit = one
    # session_exit: the first tracked SessionEnd of this codex process (our parent) claims it.
    state="${CALL_ME_BACK_STATE_DIR:-$HOME/.local/state/call-me-back}/codex-exits"
    mkdir -p "$state" 2>/dev/null
    find "$state" -mindepth 1 -maxdepth 1 -mmin +60 -exec rm -rf {} + 2>/dev/null
    proc="$PPID-$(ps -o lstart= -p "$PPID" 2>/dev/null | tr -c 'A-Za-z0-9' '_')"
    mkdir "$state/$proc" 2>/dev/null || exit 0
    printf 'Session ended (%s)' "${end_reason:-unknown}" | emit --reason session_exit --detail "$end_reason" --message-stdin
    ;;
esac
exit 0
