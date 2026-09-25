# ADR — call-me-back（索引）

決策一檔一則，放 `docs/adr/NNNN-slug.md`。名詞以 `../CONTEXT.md` 為準，需求見 `PRD.md`。

- [0001 分成兩種推送：通知與叫醒](adr/0001-two-push-modes.md)
- [0002 只提供可達性，用法交給調度者或其他 skill](adr/0002-reachability-only.md)
- [0003 叫醒由 Hermes plugin 以 `ctx.inject_message` 實作](adr/0003-wake-via-inject-message.md)
- [0004 Task 綁在派出去的那個 Session，不綁工作目錄](adr/0004-task-binds-session.md)
- [0005 綁定 Task：派工時用環境變數，接續時靠執行機記下的對話 id](adr/0005-bind-via-env-and-bindings.md)
- [0006 停下事件先進 plugin，由它決定通知與叫醒](adr/0006-events-through-plugin.md)
- [0007 調度機操作執行機：經 SSH 跑終端管理器的 CLI](adr/0007-remote-over-ssh.md)
- [0008 派工後檢查啟動畫面，並自動登記未知的目錄](adr/0008-startup-check.md)
