# 本機 Twitch 事件重播

這個入口只接受明確標示為合成資料的 EventSub 通知，不連接 Twitch、不建立訂閱、不讀寫 OAuth 憑證，也沒有公開發送器。`transport_authenticated` 固定為 false。JSON 中的 ID 符合設定，只代表本機合成綁定相符，不能作為真實 Twitch 身分驗證。

在現有 `serve` 的 retrieval、answer、entry 設定之外，明確提供 `--twitch-replay-config <binding.json>`，才會啟用 `POST /twitch/replay`。未配置時回 503；`/chat`、`/query` 的既有行為保留。不要把這個重播入口當作保護其他本機 API 的登入介面。

綁定檔使用 `schema_version: 1.0.1`、`mode: synthetic_replay_only`、`synthetic: true`，並明確指定 `subscription_id`、`broadcaster_user_id`、`reader_user_id`、`chatter_user_id`、`actor_account_id`、`entry_contexts`。後兩者必須與所有已配置主入口路徑的後端 context 完全一致；沒有私人 actor 綁定時，不能連接聊天歷史或逐字稿。context、權限、檔案路徑都不從 HTTP 事件建立，health 也不公開它們。

本次整合使用僅含 `/channel` 的 entry 設定，以及相同的合成頻道來源和用途政策。事件中的發言者 ID 是明確設定的合成帳號；頻道問答沒有 person/私人 actor 對應。其他命令不會借用既有四路測試的私人範圍。

重播控制格式為 `{ "operation": "connect", "session_id": "synthetic-session-1", "synthetic": true }`，斷線時將 operation 改為 `disconnect`。這些是本機測試控制，不是 Twitch WebSocket welcome/close 實作。通知請用 `fixtures/twitch/notification.synthetic.json` 的格式；測試時更新 UTC `message_timestamp`，允許過去 300 秒至未來 30 秒。通知中的訂閱、類型、版本、頻道、讀取帳號、發言者及 session 都須符合設定。目前只接收一致的純文字 fragments；共享聊天室來源、回覆上下文、其他訊息類型與 reconnect URL 不在支援範圍，會停止處理。

去重以聊天訊息 UUID 與頻道為穩定鍵，並另外核對投遞 ID 與事件內容雜湊。SQLite ledger 在呼叫主入口前持久化 reservation；同一事件的不同投遞 ID、斷線後重送及程序重啟後重送都不會再呼叫模型。同一 ID 的不同內容會拒絕。中斷或下游結果不確定時保留 reservation，不自動重試；這是避免重複處理的停止機制，不宣稱 exactly-once 或完成真實斷線恢復。

回答只交給既有 `DryRunOutput`，結果為 `dry_run_not_sent`、`attempted: false`、`public_reply: false`。入口拒絕或僅有證據時不產生發送意圖。原始文字、穩定 request ID、來源 reference 及綁定雜湊可串接 ingress 與主入口 trace；測試 ledger/trace 使用獨立 state-dir。

欄位依據：[EventSub WebSocket notification](https://dev.twitch.tv/docs/eventsub/websocket-reference/#notification-message)、[Channel Chat Message Event](https://dev.twitch.tv/docs/eventsub/eventsub-reference/#channel-chat-message-event)。這是支援上述子集的本機重播器，不是 Twitch SDK 或正式連線驗收。

## 運行期間的去重資料庫連續性

只有明確啟動時可以建立新的 ledger；每次處理事件都用 rw 開啟已存在的檔案，遺失時不補空檔。每筆 reservation 和完成交易都核對檔案身分、SQLite application_id、schema 版本及原有 binding。檔案被替換、綁定／標記失效或 phase 損壞時，該重播實例保持停止狀態，不自動恢復問答，也不放寬來源或 AI 用途權限。

操作人先核對並保留既有資料，再明確重新啟動隔離的重播實例，才會重新檢查可用的 ledger。既有 v1 schema 與已完成／未定 reservation 保留，不做資料重置或自動重送。檢查保護的是運行中檔案連續性；停機期間的資料回復仍須另外核對歷史，不能由此宣稱 exactly-once。