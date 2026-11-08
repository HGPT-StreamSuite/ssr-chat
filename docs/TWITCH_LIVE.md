# Twitch 接收與本機 dry-run 候選

這個入口使用真實 Twitch OAuth 與 EventSub WebSocket。離線單元測試使用替身，不構成真實登入或收訊驗收。沒有公開發送程式路徑，也未部署。

固定範圍：client `ywe81rovont7bw585tofv8jmv8xvif`、bot `dubheorigin / 99567238`、頻道與唯一測試發言者 `928757513`。首次 OAuth 僅要求 `user:read:chat`。不得把重新授權或 App 類型變更隱藏成設定修復；不支援的 App 類型應回報阻塞。

準備完整候選並透過 uv 安裝已核定依賴後，使用外部設定檔執行：

```text
uv run python -B -m ssr_chat.twitch_live login --config <external-config.json>
uv run python -B -m ssr_chat.twitch_live check --config <external-config.json>
uv run python -B -m ssr_chat.twitch_live status --config <external-config.json>
uv run python -B -m ssr_chat.twitch_live listen --config <external-config.json>
```

`login` 會顯示官方核准網址與 device user code，需要使用者以 bot 帳號完成授權。`status` 僅證明權杖身分驗證；不宣稱已收訊。權杖檔必須位於原始碼外的新路徑，不能提交至 Git。範例中的 token/state 路徑需由整合者換成這次工作區 `work/live-session/` 下的明確絕對路徑。

`check` 是離線先決條件檢查：只核對設定、固定 `websockets==17.3` 的套件資訊，以及 token/state 是否存在；不讀取或解密 token、不建立 state、不連 Twitch 或模型。`login_required` 表示可進行互動授權；`ready_for_token_validation` 只表示 token 檔存在，尚未驗證有效。這兩種狀態都不代表已登入、收訊或發送。

`listen` 最長600秒，只允許一次模型嘗試。唯一測試發言者可送 `!ask 請用一句話打招呼`；訊息不會由程式代送。接收其他使用者的通知後會丟棄文字。精確比對 OAuth client/bot、頻道、訂閱、WebSocket session、通知類型與版本、訊息時間、穩定 message ID 及發言者。拒絕自身訊息、共享聊天來源、非測試指令與未啟用的知識／天氣查詢。

只有 `transport_status.event=subscribed` 才表示已確認訂閱啟用，可提示測試者發言。`listen` 結果分開記錄 `twitch_receive_verified`、`dry_run_verified` 與固定為 false 的 `twitch_send_verified`。成功完成 dry-run 回傳 exit 0；傳輸或處理失敗回傳 exit 2；窗口結束但未完成 dry-run 回傳 exit 3，不能視為驗收通過。

通過檢查後，來源為 `twitch-eventsub`，附穩定 source_ref。新 SQLite 先持久保留操作，再交給既有 ChatRuntime；權限僅允許本機模型使用指定測試者的命令。原有 fixture 知識與天氣 adapter 明確停用，一般聊天使用本機 Ollama `qwen3.5:9b-q4_K_M`，端點固定 `127.0.0.1:11435` 並驗證模型 digest。這不代表已有真實知識、天氣或人物資料驗收。

輸出固定 DryRunOutput：只有本機回覆意圖與 trace，無 Twitch send API。已保留或結果不確定的訊息，重送與程序重啟都不再生成。一次模型失敗也會用完本次窗口，避免無人確認的自動重試。新的正式測試需另選明確的新 state 目錄；不要清除舊證據重置限額。

每次存取 receipt ledger 都核對檔案身分、owner binding、SQLite application/version 及 metadata。遺失、替換或 owner 變更會停止處理，不建立空庫代替歷史。舊候選的 owner 格式不會自動升級或清除；需保留舊證據並明確指定新的測試目錄。

會保留已接受指令的原文、本機回答與最小來源證據。未接受訊息的原文不寫入 runtime、provenance 或 CLI 輸出。token 與 runtime state 分開，不複製既有正式資料庫。既有服務、原始設定與資料不變。
