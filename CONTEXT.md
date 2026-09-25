# call-me-back

讓 Hermes agent 看得到、碰得到 CLI coding agent 的 Session，並在 Session 停下時通知人、叫醒派工對話。只提供可達性，不管怎麼用。

需求見 `docs/PRD.md`，決策見 `docs/ADR.md`。本檔只收名詞。

## 角色

**人**：
這些 Session 的擁有者。會自己開 Session，也會直接插手調度者派出去的 Session。
_Avoid_：owner

**調度者**：
負責派工的 Hermes agent。
_Avoid_：某個 agent 實例的名字、orchestrator、conductor

**執行者**：
被派工的 CLI coding agent 的種類，例如 Claude Code、Codex、pi。
_Avoid_：worker、agent（太泛；指某一次執行時用 Session）

## 機器與工具

**調度機**：
跑調度者（Hermes gateway）的機器。
_Avoid_：host、server

**執行機**：
跑 Session 的機器，可以和調度機是同一台。
_Avoid_：host、宿主、worker

**終端管理器**：
Session 所在的終端管理工具，例如 Orca、tmux。人和調度者看到、操作的是同一批 Session。
_Avoid_：宿主、host、multiplexer

## 兩端

**Session**：
某個執行者的一次執行，從啟動到退出，跑在終端管理器的一個終端裡。在裡面清空對話（例如 `/clear`）仍是同一個 Session；agent 退出後終端可能還在，但 Session 已經結束。
_Avoid_：tab、pane、terminal、任務

**接續**：
把已退出的 Session 的對話接回來（例如 `claude --resume`）所產生的新 Session。它延續原 Session 綁著的 Task，不論是誰接的。
_Avoid_：重開、restart、新 Session

**派工對話**：
發起某個 Task 的那一串 Hermes 對話，也是叫醒的對象。它對應一個聊天位置（例如一個 Telegram topic）；Hermes 在裡面換掉內部 session，仍算同一串。
_Avoid_：session（留給執行端）、thread、那串

## 工作

**Task**：
調度者派出去的一份工作，有 task id，綁在派出去的那一個 Session 和它的接續上，不綁工作目錄。從派工開始，只有被關閉才結束；Session 退出不會結束 Task。
_Avoid_：job、dispatch、任務單

**追蹤中**：
Session 綁著一個還沒關閉的 Task 的狀態。只有追蹤中的 Session 停下才會通知與叫醒；人自己開的 Session 就算在同一個工作目錄也不算。人在追蹤中的 Session 裡打字，不會改變這個狀態。
_Avoid_：tagged、有標記檔

**關閉**：
人或調度者明確結束一個 Task，這是 Task 結束的唯一方式。之後那個 Session 和它的接續停下，不再通知也不再叫醒；Session 本身不受影響。
_Avoid_：取消追蹤、完成

## 事件與推送

**停下事件**：
追蹤中的 Session 結束一個回合或退出時產生的一筆紀錄，帶有停下原因。Task 關閉之前可以產生很多筆。
_Avoid_：完成（停下不等於做完）、task-event、done

**停下原因**：
停下事件的分類：回合結束、等批准、Session 退出（正常退出或當掉都算）。
_Avoid_：status、結果

**通知**：
停下事件發生時推一則訊息給人。不經過調度者，不花 token。
_Avoid_：完成之後推給我、推給我

**叫醒**：
停下事件發生時，讓派工對話開新的一輪來接手。
_Avoid_：完成之後推給你、推給你、喚醒
