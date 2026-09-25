# Worker-side contract

Everything on the execution machine funnels into one command, `call-me-back-emit`. Each executor adapter (Claude Code hook, Codex hook, pi extension) only translates its native signal into one call of that command. Adapters never talk to the network and never decide whether a session is tracked.

## `call-me-back-emit`

```
call-me-back-emit --executor <claude-code|codex|pi> --reason <turn_end|needs_approval|session_exit>
                  [--agent-session <id>] [--cwd <dir>] [--exit-code <n>] [--detail <text>]
                  [--message-stdin]
call-me-back-emit --flush          # send spooled events (runs in the background automatically)
call-me-back-emit --forget <task>  # drop resume bindings of a closed task
```

- `--agent-session`: the executor's own conversation id (Claude Code `session_id`, Codex thread/session id, pi session id). Must stay the same when the conversation is resumed. Optional only for the wrapper's exit event.
- `--message-stdin`: the last assistant message is read from stdin (UTF-8, any length; it is truncated to 3000 chars).
- `--detail`: short machine-generated extra info (e.g. the tool waiting for approval, a SessionEnd reason).
- Always exits 0 and prints nothing on stdout, so it is safe inside any hook. It returns quickly: the network send happens in a detached background `--flush`.

### Is this session tracked?

1. `CALL_ME_BACK_TASK` is set (the dispatch wrapper exports it; hooks and extensions inherit the agent's environment): tracked. If `--agent-session` is given, the binding `(executor, agent-session) -> task` is remembered so that a later resume is recognized.
2. Otherwise, a remembered binding for `(executor, agent-session)` exists: tracked (this is a resumed session).
3. Otherwise: not tracked; exit 0 without writing anything.

Events whose `--executor` differs from `CALL_ME_BACK_EXECUTOR` (exported by the wrapper) are dropped: an agent spawned inside a dispatched Session (e.g. pi's Claude bridge starting Claude Code) inherits the environment and its hooks would otherwise speak for the Task.

Adapters own executor-specific deduplication: one process exit must become at most one `session_exit` (Codex fires SessionEnd once per thread loaded in the process).

Known gaps: a Session resumed outside the wrapper that then resets in-process (`/clear`, `/new`) gets a new conversation id and is no longer tracked; an agent of the *same* executor started as a subprocess inside a dispatched Session is indistinguishable from it.

### Reasons

| reason | when |
|---|---|
| `turn_end` | the agent finished a turn and is idle (done, or asking the human something) |
| `needs_approval` | the agent is blocked on a permission/approval prompt |
| `session_exit` | the agent process ended. Sessions started by `call-me-back-run` get this from the wrapper (covers crashes); adapters emit it only when `CALL_ME_BACK_WRAPPED` is **not** set, and never for in-process resets such as `/clear` |

### Event payload (schema v1)

```json
{
  "v": 1,
  "id": "claude-code-<session>-20260925120000-12345",
  "task": "T-20260925-foo",
  "executor": "claude-code",
  "reason": "turn_end",
  "agent_session": "…",
  "machine": "my-mac",
  "cwd": "/path/to/worktree",
  "ts": "2026-09-25T12:00:00+08:00",
  "exit_code": null,
  "detail": "",
  "last_message": "…"
}
```

`id` is unique per event and reused on every retry; the receiver deduplicates on it.

## `call-me-back-run` (dispatch wrapper)

```
call-me-back-run --task <task> --executor <executor> [--prompt-file <file>] -- <agent command…>
```

Exports `CALL_ME_BACK_TASK` and `CALL_ME_BACK_WRAPPED=1`, runs the agent (appending the prompt file's content as the last argument), and emits `session_exit` with the exit code when the agent ends for any reason.

## Receiver

The spool is flushed by piping each event JSON into `$CALL_ME_BACK_RECEIVE` (default `hermes call-me-back receive`; across machines e.g. `ssh hermes-host hermes call-me-back receive`). Exit code 0 = stored, 2 = rejected payload (dropped), anything else = keep and retry later.

Configuration lives in `~/.config/call-me-back/machine.env` (sourced by the scripts); state in `~/.local/state/call-me-back/` (`spool/`, `bindings/`, `emit.log`).
