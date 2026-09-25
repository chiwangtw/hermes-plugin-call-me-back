#!/bin/bash
# Claude Code adapter for call-me-back: Stop / Notification / SessionEnd hook -> call-me-back-emit.
# Install as a global command hook for those three events (see settings.snippet.json). Prints nothing,
# always exits 0; untracked sessions cost one jq call. Contract: machine/CONTRACT.md.

EMIT="${CALL_ME_BACK_EMIT:-$HOME/.local/bin/call-me-back-emit}"
[ -x "$EMIT" ] || exit 0

input=$(cat)
# \x1f as separator: with a tab, read would collapse empty fields and shift the rest.
IFS=$'\x1f' read -r event session cwd active ntype end_reason transcript bg <<EOF
$(printf '%s' "$input" | jq -r '[.hook_event_name // "", .session_id // "", .cwd // "",
    (.stop_hook_active // false | tostring), .notification_type // "", .reason // "",
    .transcript_path // "", ((.background_tasks // []) | length | tostring)]
    | map(tostring | gsub("[\u001f\n]"; " ")) | join("\u001f")' 2>/dev/null)
EOF

# Fast path: nothing to do unless this Session is (or was) bound to a Task.
if [ -z "${CALL_ME_BACK_TASK-}" ]; then
  state="${CALL_ME_BACK_STATE_DIR:-$HOME/.local/state/call-me-back}"
  safe_session=$(printf '%s' "$session" | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-80)
  [ -n "$session" ] && [ -f "$state/bindings/claude-code/$safe_session" ] || exit 0
fi

emit() { "$EMIT" --executor claude-code --agent-session "$session" --cwd "$cwd" "$@"; }

case "$event" in
  Stop)
    [ "$active" = "true" ] && exit 0
    msg=$(printf '%s' "$input" | jq -r '.last_assistant_message // ""')
    if [ -z "$msg" ] && [ -f "$transcript" ]; then
      # One content block per line; a reply shares message.id. Join the text blocks of the last reply.
      msg=$(jq -Rrn '
        [inputs | fromjson? | select(.type == "assistant")] as $a
        | ([$a[] | select(any(.message.content[]?; .type == "text"))] | last | .message.id) as $id
        | if $id == null then ""
          else [$a[] | select(.message.id == $id) | .message.content[]? | select(.type == "text") | .text] | join("\n\n")
          end' "$transcript" 2>/dev/null)
    fi
    detail=""
    [ "${bg:-0}" -gt 0 ] 2>/dev/null && detail="background_tasks=$bg"
    printf '%s' "$msg" | emit --reason turn_end --detail "$detail" --message-stdin
    ;;
  Notification)
    [ "$ntype" = "permission_prompt" ] || exit 0
    printf '%s' "$input" | jq -r '.message // ""' | emit --reason needs_approval --detail "permission_prompt" --message-stdin
    ;;
  SessionEnd)
    # The dispatch wrapper reports exits of the Sessions it started (crashes included); /clear is not an exit.
    [ -n "${CALL_ME_BACK_WRAPPED-}" ] && exit 0
    [ "$end_reason" = "clear" ] && exit 0
    printf 'Session ended (%s)' "${end_reason:-unknown}" | emit --reason session_exit --detail "$end_reason" --message-stdin
    ;;
esac
exit 0
