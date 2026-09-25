# Codex adapter 筆記

`hook.sh` 把 Codex 的 hook 事件翻成一次 `call-me-back-emit` 呼叫（規格見 `machine/CONTRACT.md`）。

```
Codex hook 事件            hook.sh                          call-me-back-emit
─────────────────          ──────────────────────           ─────────────────────────────
Stop              ──────►  last_assistant_message ──stdin─► --reason turn_end --message-stdin
PermissionRequest ──────►  tool_name: tool_input.command ─► --reason needs_approval --detail
SessionEnd        ──────►  WRAPPED? → 略過                  
                           同一個 codex 行程第一筆 ───────► --reason session_exit --detail other
（全部都帶 --executor codex --agent-session <session_id> --cwd <cwd>）
```

## 安裝（全域）

1. `machine/install.sh --codex …`：先把 `machine/bin`、`machine/adapters` 複製到 `~/.local/share/call-me-back/`，再呼叫 `adapters/codex/install.sh <已安裝目錄> <stamp>`。後者會把 `'<已安裝目錄>/hook.sh'` 加進 `${CODEX_HOME:-~/.codex}/hooks.json` 的 Stop、PermissionRequest（timeout 10）和 SessionEnd（timeout 3），保留既有的 Orca hook。它會先備份成 `hooks.json.bak-call-me-back-<stamp>`，重跑不會重複加。已在 `~/.codex/hooks.json` 的副本上跑過兩次：Stop、PermissionRequest 變成 2 組，SessionEnd 新增 1 組，其他事件不變；沒有 hooks.json 時也能從零建立。
   `hooks.json` 是手動安裝用的樣板（`"$HOME/.local/share/call-me-back/adapters/codex/hook.sh"`）。`hook.sh` 找 emit 的順序：`$CALL_ME_BACK_EMIT` → PATH 上的 `call-me-back-emit` → `~/.local/bin/call-me-back-emit`。
2. 手動安裝（不經 install.sh）：
   ```sh
   cp ~/.codex/hooks.json ~/.codex/hooks.json.bak
   jq -s '.[0] as $cur | .[1] as $add | $cur | .hooks = (($cur.hooks // {}) as $h
         | reduce ($add.hooks | keys[]) as $k ($h; .[$k] = ((.[$k] // []) + $add.hooks[$k])))' \
      ~/.codex/hooks.json.bak machine/adapters/codex/hooks.json > ~/.codex/hooks.json
   ```
3. 互動式開一次 `codex`，出現「Hooks need review」時選信任（或用 `/hooks` 逐一檢查）。Codex 會把 `trusted_hash` 寫進 `~/.codex/config.toml` 的 `[hooks.state."<hooks.json 路徑>:<event>:<group>:<index>"]`。沒信任的 hook 不會跑，`codex exec` 也不會跳出詢問。
4. 如果派工的 codex 是由 Orca 啟動：Orca 用另一個 CODEX_HOME（`~/Library/Application Support/orca/codex-runtime-home/home/`，裡面有 Orca 自己管的 `hooks.json` 和 `.orca-hook-trust-provenance.json`），同一組 hook 也要加進去：`CODEX_HOME="$HOME/Library/Application Support/orca/codex-runtime-home/home" machine/adapters/codex/install.sh ~/.local/share/call-me-back/adapters/codex`。Orca 會不會覆寫那份檔案：UNVERIFIED。在一般終端機直接執行 `codex` 時，沒有設 CODEX_HOME（這台機器的 shell 實測如此）。

## 已驗證的事實（codex-cli 0.154.0，2026-09-25 實跑）

測試方式：在 tmux 裡跑互動 TUI（`tmux new -d` + `send-keys` + `capture-pane`），另外跑兩次 `codex exec`。沿用真正的 `~/.codex`（這樣 ChatGPT OAuth 的 token refresh 會照常寫回原本的 auth.json），hook 放在 scratch 專案的 `.codex/hooks.json`，只用這次執行有效的旗標讓它載入：

```
codex --dangerously-bypass-hook-trust \
      -c 'projects={"<scratch 專案絕對路徑>"={trust_level="trusted"}}' \
      -c check_for_update_on_startup=false -c 'model_reasoning_effort="low"' [PROMPT]
```

`~/.codex/config.toml` 和 `~/.codex/hooks.json` 前後的 sha1 一致，沒有改到。注意：`-c 'projects."<dir>".trust_level="trusted"'` 這種 dotted 寫法無效，TUI 仍會跳出信任詢問；要用上面的 inline table 寫法。沒採用「另設一個 CODEX_HOME 再複製 auth.json」的做法：access token 當天就過期、last_refresh 已經 10 天，副本一旦 refresh 會輪替 refresh token，原本的 auth.json 可能因此失效。

### 事件的原始 payload 欄位（實際收到的）

| 事件 | 欄位 |
|---|---|
| `SessionStart` | `session_id, transcript_path, cwd, hook_event_name, model, permission_mode, source`（實際看到 `startup` / `resume` / `clear`；schema 另有 `compact`） |
| `UserPromptSubmit` | `session_id, turn_id, transcript_path, cwd, hook_event_name, model, permission_mode, prompt` |
| `Stop` | `session_id, turn_id, transcript_path, cwd, hook_event_name, model, permission_mode, stop_hook_active, last_assistant_message` |
| `PermissionRequest` | `session_id, turn_id, transcript_path, cwd, hook_event_name, model, permission_mode, tool_name`（`"Bash"`）`, tool_input`（`{"command":"touch approval-test.txt","description":"adapter test"}`） |
| `SessionEnd` | `session_id, transcript_path, cwd, hook_event_name, reason`（永遠是 `"other"`；binary 內嵌的 schema 把它定成 `const "other"`） |

0.154 binary 內嵌 schema 裡的其他事件：`PreToolUse, PostToolUse, PreCompact, PostCompact, SubagentStart, SubagentStop, Interrupt`。沒有 Claude Code 的 `Notification`，所以等待核准只能靠 `PermissionRequest`。

### 行為

- **環境變數會繼承**：hook 收得到 `CALL_ME_BACK_TASK`、`CALL_ME_BACK_WRAPPED`（TUI 和 exec 都一樣）。hook 的父行程就是 codex binary 本身，單一指令和 `a && b` 複合指令都是。
- **hook 指令經過 shell**：`[ -n "$HOME" ] && …` 這種寫法可以正常執行，所以 `hooks.json` 裡可以寫 `$HOME/...`。
- **stdout**：Codex 會把 hook 的 stdout 當成該事件的 JSON 輸出來解析。`hook.sh` 不輸出任何東西；實測 PermissionRequest hook 跑完後，核准畫面照樣出現給人（「Would you like to run the following command? … 1. Yes, proceed (y)」）。
- **timeout**：`SessionEnd` 和 `Interrupt` 的 hook timeout 會被壓到 3 秒（啟動時顯示 `clamping SessionEnd hook timeout to 3s`），所以 `hooks.json` 裡 SessionEnd 直接寫 3。
- **SessionStart 延後觸發**：TUI 啟動時不會觸發，要等第一個 prompt 送出才會觸發。
- **接續時 id 不變**：`codex resume <session-id> "reply with the word ok"` 的 `session_id` 和原本相同，`SessionStart.source = "resume"`。沒有 `CALL_ME_BACK_TASK` 時，Stop 靠 binding 被認出來並送出 `turn_end`；退出時送出一筆 `session_exit`。
- **`/clear`**：當下不會觸發 SessionEnd。之後會換成新的 thread id，下一個 prompt 才觸發 `SessionStart(source=clear)`（新 id）。**行程退出時，這個行程載入過的每個 thread 都會各觸發一次 SessionEnd**，包含 `/clear` 之前的舊 thread，以及還沒跑過 turn 的 thread（實測 12:31:22、12:31:23 連續兩筆）。所以 `hook.sh` 用 `$PPID` 加上行程啟動時間，在 `$CALL_ME_BACK_STATE_DIR/codex-exits/` 下 mkdir 一個標記，讓同一個 codex 行程只送一筆 `session_exit`。標記保留 60 分鐘後清掉。
- **`CALL_ME_BACK_WRAPPED=1`**：SessionEnd 照樣觸發，`hook.sh` 不送 `session_exit`（實測 run 5）。
- **`codex exec`**：SessionStart、UserPromptSubmit、Stop、SessionEnd 都會觸發。exec 的 approval 是 `never`，所以不會有 PermissionRequest。
- **帶初始 prompt 開 TUI**：`codex "reply with the word ok"`（run 5）、`codex resume <id> "reply with the word ok"`（run 4）都驗證過。`call-me-back-run … -- codex` 把 prompt 接在最後一個參數，形式正好相同。

### UNVERIFIED

- `/new`（或其他會換 thread 的指令）是否和 `/clear` 行為相同。
- 按 Esc 中斷 turn 時，Stop 會不會觸發（schema 另外有 `Interrupt` 事件，這裡沒有接）。
- 使用者 alias 帶的 `--dangerously-bypass-approvals-and-sandbox` 之下，PermissionRequest 應該永遠不會觸發（因為不會有核准畫面），沒有實測。
- trust hash 涵蓋的範圍：推測只算 `hooks.json` 那一筆設定，不含 `hook.sh` 的內容，所以改 script 不必重新信任。測試全程都用 `--dangerously-bypass-hook-trust`，沒有驗證。
- Orca 的 codex runtime home 會不會覆寫 `hooks.json`。

## 已知缺口

- **沒經過 wrapper 的接續裡做 `/clear`**：新 thread 沒有 env，也沒有 binding，所以不算追蹤中。Codex 退出時提示的 `codex resume <新 id>` 也接不回 Task。派工時的 Session（有 env）沒有這個問題：新 thread 的第一個 Stop 就會綁上。修法見最終報告裡的 contract 提案（`--link`）。
- 派工的 Session 在 `/clear` 之後一個 turn 都沒跑就退出：新 thread 沒有 binding，之後 resume 它也不算追蹤中。
