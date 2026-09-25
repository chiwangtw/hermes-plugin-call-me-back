# 0007 調度機操作執行機：經 SSH 跑終端管理器的 CLI

**Status**：accepted（2026-09-25）

列出、讀取、送字、派工，都要在調度機上操作執行機的終端管理器。參考部署的調度機是 Windows、執行機是 macOS；中間要經過 Windows 的命令列、OpenSSH 和遠端的 shell，送出的文字又常含引號與中文。

做法：

- plugin 在執行機上跑終端管理器的 CLI（目前是 Orca 的 `orca … --json`）。同機時直接用 `/bin/sh -c`，跨機時用 `ssh -o BatchMode=yes <host>`。
- 跨機時，整段 shell 指令（連同要餵給它的內容）先以 base64 編碼放在 ssh 命令列上，在執行機以 `base64 -d | /bin/sh` 還原後執行。超過單一命令列的長度就分段上傳成暫存檔，再一次執行。
- ssh 的 stdin 接 NUL，stdout 和 stderr 寫到暫存檔，不用 pipe。
- 呼叫端給的文字（送出的字、標題、路徑、task id）一律以 shell 字面引號包起來。只有使用者設定裡的路徑（`orca_bin`、`bin_dir`、`state_dir`）會把開頭的 `~/` 展開成執行機的 `$HOME`。
- 派工的 prompt 先寫成執行機上的檔案，再由 `call-me-back-run` 讀進來當 agent 的最後一個參數；太大時改成請 agent 自己讀那個檔案。
- Orca 回 JSON 錯誤時，把錯誤碼和它建議的下一步交給調度者，而不是一段截斷的輸出。

為什麼：

- Windows 會重新跳脫命令列，弄壞引號與中文；base64 只有 ASCII 字元，經過哪一層都不會變。
- Windows OpenSSH 在 stdin 或 stdout 是 pipe 時會卡住。
- 字面引號讓工具的輸入（可能轉述自執行者的輸出）永遠不會在執行機被當成指令執行。
- 很長的 prompt 經過 ssh、Orca 再到 argv，每一層都有長度限制和跳脫問題；放成檔案最穩。
- 直接用終端管理器現有的 CLI，執行機不必多跑常駐服務，也不必開 port。

代價：

- 每次操作都開一條新的 SSH 連線。
- 執行機要有 POSIX shell 和 `base64`。
- 目前只接 Orca；其他終端管理器要另寫一層對應的指令。
