# pi adapter 筆記

`call-me-back.ts` 是 pi extension，把 pi 的事件翻成一次 `call-me-back-emit` 呼叫（規格見 `machine/CONTRACT.md`）。

```
pi 事件                          call-me-back.ts                           call-me-back-emit
─────────────────────            ─────────────────────────────             ─────────────────────────────
agent_settled           ───────► 最後一則 assistant 文字 ─── stdin ──────► --reason turn_end --message-stdin
  （stopReason=aborted 略過；error 且沒文字時送 "[error] <errorMessage>"，--detail error）
ui_prompt_start         ───────► 只在 agent 執行中才送 ────────────────────► --reason needs_approval --detail "<kind>: <title>"
session_shutdown        ───────► reason=quit 且沒有 CALL_ME_BACK_WRAPPED ──► --reason session_exit --detail quit
  （new / resume / fork / reload 是同一個行程內換 session，不送）
（全部都帶 --executor pi --agent-session <sessionManager.getSessionId()> --cwd <ctx.cwd>）
```

- emit 的路徑：`$CALL_ME_BACK_EMIT`，沒有就用 PATH 上的 `call-me-back-emit`。
- 用 `spawn(…, {detached: true})` 加 `unref()` 啟動子行程，pi 不會等它。訊息先寫進暫存檔，打開後立刻 unlink，再把 fd 當成子行程的 stdin。這樣是同步交出去的，pi 馬上退出（print mode、quit）也不會漏掉。
- **保序**：如果上一個 emit 還沒結束，下一個會包在 detached 的 `/bin/sh` 裡，先等上一個結束（最多約 5 秒）再 exec。原因：print mode 的 `agent_settled` 和 `session_shutdown(quit)` 在同一毫秒觸發，第一版實測 `session_exit` 比 `turn_end` 先進 spool、也先送達。
- **快速路徑**：沒有 `CALL_ME_BACK_TASK`，也沒有 `bindings/pi/<id>` 時，完全不 spawn。

## 安裝（全域）

```sh
machine/install.sh --pi …     # 會呼叫 adapters/pi/install.sh <已安裝目錄> <stamp>
# 或手動：
PI_CODING_AGENT_DIR=~/.pi/agent machine/adapters/pi/install.sh ~/.local/share/call-me-back/adapters/pi
```

`install.sh` 會把 `<已安裝目錄>/call-me-back.ts` 加進 `~/.pi/agent/settings.json` 的 `"extensions"` 陣列：先備份成 `settings.json.bak-call-me-back-<stamp>`，重跑不會重複加。在 scratch 的 agent dir（`PI_CODING_AGENT_DIR` 指過去，加 `PI_OFFLINE=1`）驗證過，啟動畫面的 `[Extensions]` 會列出 `call-me-back.ts`。另一種做法：放進（或 symlink 到）`~/.pi/agent/extensions/call-me-back.ts`。只對之後新開的 pi 生效，已開的可以 `/reload`。Orca 直接把自己的 extension 寫進 `~/.pi/agent/extensions/`，沒有另設 agent dir，所以 Orca 開的 pi 也會載入這個 adapter（推論，UNVERIFIED）。

## 已驗證的事實（pi 0.85.1，2026-09-25 實跑）

測試方式：用 `pi -e <adapter> -e <scratch 事件記錄 + bash 確認閘門>` 載入。CLI `-e` 不需要專案信任，也不動全域設定；`~/.pi/agent/settings.json` 和 `extensions/` 前後 sha1 一致。模型用一個 Claude 模型（經 provider extension）。run 1 是 print mode（`-p`）；run 2 到 5 在 tmux 裡跑互動 TUI（`tmux new -d` + `send-keys` + `capture-pane`），共 5 次模型呼叫。

### 事件 payload（實際收到的欄位）

| 事件 | 欄位 |
|---|---|
| `session_start` | `type, reason`（實測 `startup`、`new`），`/new` 時多 `previousSessionFile` |
| `session_before_switch` | `type, reason`（`new`） |
| `session_shutdown` | `type, reason`（實測 `quit`、`new`），`/new` 時多 `targetSessionFile` |
| `agent_start` | `type` |
| `turn_end` | `type, turnIndex, message, toolResults` |
| `agent_end` | `type, messages` |
| `agent_settled` | `type`（沒有其他欄位，所以最後一則訊息要從 `ctx.sessionManager.getBranch()` 倒著找 `type=="message" && message.role=="assistant"`） |
| `ui_prompt_start` / `ui_prompt_end` | `type, reason`（`"ui_prompt"`）`, kind`（`"confirm"`）`, title`（`"Allow bash?"`） |

`ctx`：`sessionManager.getSessionId()`、`sessionManager.getSessionFile()`、`mode`（`"tui"` / `"print"`）、`isIdle()`（`agent_settled` 時為 true，執行中為 false）、`cwd`。一輪的順序：`agent_start → turn_end(×N) → agent_end → agent_settled`。

### 行為

- **環境變數會繼承**：extension 跑在 pi 行程裡，`process.env` 裡有 `CALL_ME_BACK_TASK` 和 `CALL_ME_BACK_WRAPPED`（TUI 和 print mode 都實測過），spawn 出去的 emit 也會繼承。
- **接續時 id 不變**：`pi --session <id> "reply with the word ok"`（run 4，沒帶 env，靠 binding 被認出來：送出 `turn_end`，退出時送 `session_exit`）；`pi -c "reply with the word ok"`（run 5）都沿用原本的 session id。注意這兩種啟動時 `session_start.reason` 都是 `"startup"`，不是 `"resume"`；`"resume"` 只出現在行程內的 `/resume`。
- **`/new`**：`session_before_switch(new)` → `session_shutdown(reason=new, targetSessionFile)` → `session_start(reason=new, previousSessionFile)`，換成新的 session id，不送 `session_exit`。
- **退出（Ctrl+D）**：只有當下的 session 觸發 `session_shutdown(reason=quit)`。`/new` 之前的 session 已經在切換時收過 `reason=new`，不會像 Codex 那樣退出時再補一次。`CALL_ME_BACK_WRAPPED=1` 時不送（run 5）。
- **print mode**：extension 照樣載入，`agent_settled` 和 `session_shutdown(quit)` 都會觸發（run 1）。
- **核准**：pi 本身沒有核准畫面。0.85.1 的 dist 裡沒有 `tool_approval_*` 事件（那是 OMP fork 才有的；Orca 的 extension 也只在 OMP runtime 聽它）。唯一的訊號是其他 extension 開的對話框觸發的 `ui_prompt_start`。用 scratch 閘門（`tool_call` 裡對 bash 呼叫 `ctx.ui.confirm("Allow bash?", command)`）實測：送出 `needs_approval`，detail 是 `confirm: Allow bash?`，選 Yes 之後照常送 `turn_end`。
- **帶初始 prompt 開 TUI**：`pi "reply with the word ok"`（run 2）、`pi --session <id> "…"`、`pi -c "…"` 都驗證過。
- **pi 透過會啟動 Claude Code 子行程的 provider extension 使用 Claude 模型時，會多出幽靈 `claude-code` 事件（實測，run 5）**：那個 extension 的主路徑會 spawn Claude Code 子行程，`settingSources` 用預設值，所以會載入 `~/.claude/settings.json` 的 hooks；env 又繼承自 pi。run 5 當時 Claude Code adapter 剛被裝進全域（12:40:16），結果 pi 每跑一輪，除了 `executor=pi` 的 `turn_end`，還多一筆 `executor=claude-code` 的 `turn_end`（session `b8876592-…`，cwd 是 pi 的專案，last_message `"ok"`），也多寫了一份 `bindings/claude-code/<id>`。沒設 `CALL_ME_BACK_WRAPPED` 時，理論上每輪也會多一筆 `session_exit`（UNVERIFIED）。修法見最終報告的 contract 提案（emit 依 `CALL_ME_BACK_EXECUTOR` 過濾）。

### UNVERIFIED

- 按 Esc 中止（`stopReason: "aborted"`）時不送 `turn_end`：只用假 ctx 的 harness 驗證過，沒有實跑。
- `/resume`、`/fork`、`/clone`、`/reload` 的 `session_shutdown.reason`：依原始碼（`dist/core/agent-session-runtime.js` 的 `teardownCurrent(reason)`、`agent-session.js` 的 `reason: "reload"`）判斷不是 `quit`，所以不送；沒有實跑。
- `pi -r`（picker）選同一個 session 後 id 不變：推論和 `--session` 相同，沒有實跑。
- SIGHUP、SIGTERM（終端被關）時會不會觸發 `session_shutdown(quit)`：文件和 interactive-mode 的註解都說會；沒有驗證。

## 已知缺口

- **沒經過 wrapper 的接續裡做 `/new`**：新 session 沒有 env，也沒有 binding，所以不算追蹤中。pi 的 `session_start` 有給 `previousSessionFile`，如果 emit 支援「把舊 id 的 binding 複製給新 id」，這裡可以補上（見最終報告的 `--link` 提案）。
- 只要 extension 會開對話框，就算是和核准無關的問題（例如 select），agent 執行中開的也會被當成 `needs_approval`。
