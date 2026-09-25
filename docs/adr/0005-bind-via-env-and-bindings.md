# 0005 綁定 Task：派工時用環境變數，接續時靠執行機記下的對話 id

**Status**：accepted（2026-09-25）

0004 要求 Task 綁在派出去的那個 Session 以及它的接續上。判斷由執行機上的單一指令 `call-me-back-emit` 負責；各執行者的轉接（Claude Code hook、Codex hook、pi extension）只把原生訊號翻成一次 emit 呼叫，不碰網路，也不判斷是否追蹤。規格見 `machine/CONTRACT.md`。

做法：

- **派工**：調度者用派工 wrapper `call-me-back-run` 啟動執行者，export `CALL_ME_BACK_TASK`、`CALL_ME_BACK_WRAPPED=1`、`CALL_ME_BACK_EXECUTOR`。執行者的 hook 或 extension 會繼承環境變數，所以 `/clear` 之後仍算追蹤中。
- **接續**：第一筆帶對話 id 的停下事件發生時，執行機記下「(執行者, 執行者自己的對話 id) → Task」。Claude Code 的 `--resume`（不加 `--fork-session`）、Codex 的 `resume`、pi 的 `--session`／`-c` 都沿用原本的對話 id，所以之後即使沒有環境變數，也能靠這份記錄認出接續。
- **Session 退出**：由 wrapper 回報，正常結束、當掉、終端被關掉都會回報，並帶 exit code。沒經過 wrapper 的接續，才改由執行者自己的 SessionEnd 類事件回報；`/clear` 這類行程內重設不算退出。一次行程退出最多一筆，重複觸發由各轉接自己去重（Codex 退出時會對載入過的每個對話各觸發一次）。
- **關閉**：plugin 請執行機執行 `call-me-back-emit --forget <task>`，清掉綁定記錄與派工時留下的 prompt 檔。
- **巢狀 agent**：事件的執行者和 `CALL_ME_BACK_EXECUTOR` 不同就丟掉。派出去的 Session 裡再啟動的 agent（例如 pi 經 Claude bridge 啟動 Claude Code）會繼承環境變數，它的 hook 不能代表這個 Task 發言。

考慮過但沒採用：

- 工作目錄標記檔：以目錄為單位，會把人在同一目錄開的 Session 也算進去（見 0004）。
- 終端管理器的 terminal handle：只有 Orca 有，而且接續常常發生在另一個終端裡。

已知缺口：

- 沒經過 wrapper 的接續，若在行程內重設對話（`/clear`、`/new`），會換成新的對話 id，之後不再追蹤。
- 派出去的 Session 在 `/clear` 之後一個回合都沒跑就退出：新對話 id 沒有綁定，之後接回來也不算追蹤中。
- 派出去的 Session 裡以子行程啟動**同種**執行者，無法和它區分。
- 人在別處接續的 Session 如果是當掉而不是正常退出，不會產生 Session 退出事件。
