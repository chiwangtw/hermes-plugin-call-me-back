# 0006 停下事件先進 plugin，由它決定通知與叫醒

**Status**：accepted（2026-09-25）

停下事件在執行機產生，通知與叫醒發生在調度機。兩台機器之間可能斷線，gateway 可能正在重啟；同一筆事件可能送兩次。而「Task 關閉後不再通知也不再叫醒」需要一個知道 Task 狀態的判斷點。

做法：

```
執行機                                              調度機
轉接 ─► call-me-back-emit ─► spool ─(背景送出)─► hermes call-me-back receive ─► inbox
         判斷是否追蹤          同機直接執行，跨機經 ssh                              │
                                                                  gateway 內的 watcher
                                                          去重 → 找 Task → 記錄 → 叫醒 → 通知
```

- **執行機**：emit 把事件寫進 spool 就返回（永遠 exit 0、不輸出任何東西），傳送交給脫離的背景程序，不拖住 agent。背景程序從舊到新逐件把事件 JSON pipe 給 `hermes call-me-back receive`：同機直接執行，跨機經 `ssh <調度機>` 執行（`CALL_ME_BACK_RECEIVE`）。前一筆沒送到，後面的不會越過它。
- **執行機送不出去時**：背景以退避重試（15 秒起，間隔最長 5 分鐘，預設持續 2 小時），之後交給下一次 emit。receive 回 0 表示已收下、回 2 表示格式錯誤（不再送），其他都保留重送。
- **receive**：只驗證格式、把事件落進 plugin 的 inbox。同一個事件 id 還在 inbox 時，重送會被忽略。
- **watcher**：在 gateway 行程裡逐件處理。以事件 id 去重；Task 不存在就把事件放到一旁，已關閉就丟掉；然後記錄、叫醒、通知。每完成一步就寫回事件檔，所以重試、當掉、gateway 重啟都不會重複叫醒或重複通知。失敗以指數退避重試（間隔最長 5 分鐘），連續失敗一小時後把事件放到一旁。
- **gateway 排空時暫停**：gateway 準備重啟時，`inject_message` 仍會回成功，但注入會被丟掉。所以排空期間 watcher 不處理，事件留在 inbox 等 gateway 回來。
- 通知以事件 id 當 `X-Request-ID`。

為什麼：

- 只有 plugin 知道 Task 的狀態，關閉後不再推送的判斷只能放在這裡。讓執行機直接打通知的 route 做不到。
- Session 的最後一次停下可能正好送不出去，之後不會再有停下來補送它，所以執行機要自己在背景重試。
- gateway 停機時事件留在 inbox，恢復後補處理，不會遺失。
- 傳輸只用調度機本來就接受的 SSH（或同機直接執行）。Hermes 的 webhook 只綁 `127.0.0.1`，不必開防火牆或對外開 port。
- 接收端只依賴 Hermes 本身，不依賴特定作業系統的 shell。

考慮過但沒採用：

- 執行機直接 POST 到調度機的 webhook：要對外開 port，也繞過了 Task 狀態的判斷。
- 用聊天群組當觸發：bot 收不到其他 bot 在群組裡的訊息；改用人的帳號發，又等於冒用人的身分。

代價：

- 跨機時，執行機要能免互動 ssh 到調度機。調度機是 Windows 時，receive 指令要以 `; exit $LASTEXITCODE` 結尾，否則 PowerShell 會把 exit code 2 變成 1。
- 放到一旁的事件不會再處理，要人去 plugin 資料目錄的 `rejected/` 看原因。
