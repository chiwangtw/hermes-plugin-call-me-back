# call-me-back

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that lets Hermes dispatch work to
CLI coding agents — **Claude Code, Codex and pi** — running in [Orca](https://github.com/stablyai/orca) on
another (or the same) machine, and **calls Hermes back** whenever a dispatched session stops.

- **Notify**: you get a zero-token message on your chat platform (Telegram, Discord, Slack… anything a
  Hermes `deliver_only` webhook route can deliver to).
- **Wake**: the *same* Hermes conversation that dispatched the work gets a new turn and takes over — reads
  the screen, answers the agent, or closes the task. No polling, no "I'll check later".

Hermes' built-in coding-agent skills can drive an agent, but only by polling a terminal; they get no stop
event. call-me-back adds that callback, for three agents, across machines.

```
 Hermes machine                                        Execution machine (Orca)
┌──────────────────────────────────────────┐   ssh   ┌──────────────────────────────────────────┐
│ call-me-back plugin                       │ ──────> │ orca terminal list/read/send/create       │
│  tools: list read send dispatch tasks     │         │                                          │
│         close                             │         │ call-me-back-run  (starts the agent,     │
│                                           │         │   binds it to the Task, reports its exit) │
│  inbox <── hermes call-me-back receive ◄──┼─────────┼── call-me-back-emit ── spool (retries)    │
│   │                                       │   ssh   │      ▲                                   │
│   ▼ watcher (only for open Tasks)         │         │      │ adapters                          │
│   ├─ deliver_only route ──> you (notify)  │         │      ├ Claude Code: Stop/Notification/   │
│   └─ inject_message ──> the dispatching   │         │      │              SessionEnd hooks     │
│        conversation (wake)                │         │      ├ Codex: Stop/PermissionRequest/    │
└──────────────────────────────────────────┘         │      │        SessionEnd hooks           │
                                                      │      └ pi: extension                     │
                                                      └──────────────────────────────────────────┘
```

Status: **v0.1**. Verified end to end with Hermes 0.21.3 on Windows driving Orca on macOS, for all three
agents. Same-machine setups work too (`host: local`). tmux is not supported yet.

## How a Task behaves

- `call_me_back_dispatch` starts an agent in a new Orca terminal and creates a **Task** bound to that
  session. End the turn; you will be called back.
- Every stop of that session — *turn ended*, *waiting for approval*, *session exited* — notifies you and
  wakes the dispatching conversation, **until the Task is closed** (`call_me_back_close`).
- Resuming the session later (`claude --resume`, `codex resume`, `pi --session`) continues the same Task.
- Sessions you start yourself are never reported, even in the same directory.
- Stop events survive network drops: they are spooled on the execution machine and retried; the plugin
  deduplicates by event id.

## Requirements

- Hermes Agent with plugin message injection (`ctx.inject_message`), e.g. 0.21.x, running a gateway.
- Execution machine: macOS or Linux with Orca, `jq` and `perl`, plus any of Claude Code, Codex (≥ 0.154,
  hooks), pi (≥ 0.85).
- If the two machines differ: SSH from the Hermes machine to the execution machine (for the tools) and
  back (for stop events), both non-interactive (keys, `BatchMode`).

## Install

### 1. Hermes machine

```sh
hermes plugins install chiwangtw/hermes-plugin-call-me-back --enable
```

Add to `config.yaml`:

```yaml
plugins:
  entries:
    call-me-back:
      allow_gateway_injection: true        # required for waking the conversation
      settings:
        host: my-mac                       # ssh alias of the execution machine; `local` if it is this machine
        orca_bin: /Applications/Orca.app/Contents/Resources/bin/orca
        notify_route: call-me-back-notify  # deliver_only webhook route; leave empty to skip notifications
        language: en                       # en | zh-TW (notification and wake-up text)
        # executors:                       # optional: override the command per agent
        #   codex: [codex, --dangerously-bypass-approvals-and-sandbox]
```

Create the notification route (it delivers the plugin's text as is, without running the agent):

```sh
hermes webhook subscribe call-me-back-notify --deliver telegram --deliver-chat-id <chat id> \
  --deliver-only --prompt '{text}' --description 'call-me-back notifications'
```

Restart the gateway.

### 2. Execution machine

```sh
git clone https://github.com/chiwangtw/hermes-plugin-call-me-back
cd hermes-plugin-call-me-back
machine/install.sh --receive "ssh hermes-host hermes call-me-back receive" --claude-code --codex --pi
```

- `--receive` is how stop events reach Hermes. On the same machine, omit it (default:
  `hermes call-me-back receive`). If the Hermes machine is **Windows** (PowerShell as the SSH shell), use
  `--receive "ssh hermes-host 'hermes call-me-back receive; exit \$LASTEXITCODE'"` so exit codes survive.
- Codex runs new hooks only after you trust them: start `codex` once and accept *Hooks need review*.
- Every settings file the installer edits is backed up as `<file>.bak-call-me-back-<timestamp>`.

## Using it

Just ask Hermes, for example: *"Dispatch a codex task in ~/src/app: fix the flaky login test. Close it when
the tests pass."* Hermes calls `call_me_back_dispatch`, ends its turn, and continues when Codex stops.

The plugin ships a skill with the details for the agent: `skill_view('call-me-back:guide')`.

| Tool | Does |
|---|---|
| `call_me_back_list` | List terminals on the execution machine and which Task they belong to |
| `call_me_back_read` | Read a session's screen |
| `call_me_back_send` | Type into a session (Enter by default) or interrupt it |
| `call_me_back_dispatch` | Start an agent with a prompt and create a Task (existing directory, or a new Orca worktree) |
| `call_me_back_tasks` | List Tasks and their last stop |
| `call_me_back_close` | Close a Task: the session keeps running, but no more callbacks |

## Security notes

- The wake-up is injected into the conversation **as user input**. The executor's last reply inside it is
  clearly marked as data, but it is still text written by another model: keep your orchestrator's usual
  prompt-injection hygiene.
- `allow_gateway_injection` lets this plugin start turns in existing conversations. Grant it only if you
  trust the code.
- The tools run `orca` over SSH on the execution machine. Text you pass (prompts, messages) is always
  quoted literally; only configured paths expand `~/`.

## Layout

| Path | What |
|---|---|
| `plugin.yaml`, `__init__.py`, `remote.py`, `store.py`, `events.py` | The Hermes plugin |
| `skills/guide/` | Skill bundled with the plugin |
| `tests/` | Unit tests (no Hermes needed): `python -m pytest tests -q` |
| `machine/` | Execution-machine side: `bin/`, `adapters/`, `install.sh`, `CONTRACT.md` |
| `CONTEXT.md`, `docs/` | Glossary, PRD and ADRs (in Traditional Chinese) |

## License

MIT
