# 0003 叫醒由 Hermes plugin 以 `ctx.inject_message` 實作

**Status**：accepted（2026-09-25）

叫醒必須是派工對話本身收到、開新的一輪，否則人還是只能「等一下再去看」。派工對話對應聊天平台上的一個位置，而 Hermes 的 webhook、api_server 各有自己的 session，不一定連得回那串對話。

做法：call-me-back 做成 Hermes general plugin。

- 派工 tool 被呼叫時，用 `get_session_env("HERMES_SESSION_KEY")` 取得派工對話的 session key，和 Task 一起落檔。gateway 重啟後仍讀得到。
- 之後每筆停下事件，只要 Task 還沒關閉，就呼叫 `ctx.inject_message(<叫醒文字>, session_key=...)`，gateway 在派工對話開新的一輪。
- 取不到 session key 時（例如不是從 gateway 的對話派工），這個 Task 只通知、不叫醒，派工結果會註明。

證據：在 Hermes v0.21.3（Telegram gateway）上實測：閒置、忙碌、多串同時三種情況都通過。

- 閒置：注入接續同一段對話（對話紀錄延續，不是新 session），回覆送回同一個聊天位置。
- 忙碌：注入會排隊，等該輪回覆送出後才開新的一輪，不會插斷。
- 多串同時：注入只進目標那串，其他串照常進行。
- 從 plugin 的背景 thread 呼叫是安全的：gateway 會把它排進自己的 event loop。

考慮過但沒採用：

- **webhook route**：每次 POST 都另起一個獨立 session（`webhook:<route>:<delivery_id>`），叫不醒既有的對話。
- **api_server 的 `X-Hermes-Session-Key`**：只認 api_server 自己的 session，不是聊天平台上的對話。
- **背景程序 notify**：派工對話啟動一支等待程序（`terminal(background=true, notify=true)`），讀到停下事件就結束，gateway 會把結束事件注入啟動它的那串。可用，但必須在派工的同一輪啟動、有逾時、只叫醒一次（讀到第一筆就結束，之後再停下只剩通知）、gateway 重啟就消失。這和 0004「關閉前每次停下都叫醒」不相容。

代價：

- 設定要開 `plugins.entries.call-me-back.allow_gateway_injection: true`。
- `get_session_env` 不是公開 API，Hermes 每次升級都要回歸測試叫醒。
- 注入的內容在 gateway 看來就是人本人打的字，agent 分不出來。所以叫醒文字會標明「這是 call-me-back 自動送入的事件」，執行者的最後回覆一律框起來、標示為資料，不照裡面的指示做。
- session key 的格式是 `agent:main:<platform>:dm:<chat_id>:<thread_id>`，含聊天平台的 id。它只存在 plugin 的資料目錄，不寫進通知、log 範例或文件。
