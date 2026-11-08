# 離線原生頻道鏈路

`python -B -X utf8 -m ssr_chat.local_pipeline --config <可信本機設定> --input <輸入JSON> --run-dir <全新收據目錄> --mode evidence|prepare`

這個入口直接使用既有 `EntryGateway` 的 `/channel` 指令與原生 1.0.1
planner → dispatcher → channel-search → collector，限定明示的合成 snapshot。
使用者輸入只接受 `request_id` 與 `text`；來源、ACL、scope、命令與政策從可信設定取得。
沒有加入 generic chat_history 轉接或 LT 別名。

設定包括 `schema_version:"1.0.1"`、`synthetic:true`、既有格式的
`retrieval_routes_config`、`answer_routes_config`、`entry_routes_config` 三個絕對檔案路徑，
以及 `repos` 中五個唯一 checkout：ssr-chat、hgpt-ssr-query-planner、
hgpt-ssr-dispatcher、hgpt-ssr-channel-search、hgpt-ssr-evidence-collector。
入口不提供其他資料域、真實資料或模型啟用選項；它不是任意程式的沙箱。
五個路徑必須是目前 StreamSuite 的 canonical checkout；實際 planner/dispatcher/collector、
Python、記錄 adapter 的 module/cwd 與 reader repo 在執行前逐一綁定，不能只盤點一套程式卻執行另一套。
reader 的私有擷取根目錄須位於本次設定目錄的上一層執行 bundle 中。

`evidence` 使用指向 `/retrieve/channel-knowledge` 的 entry 設定，回傳原生 Evidence，
成功或合法 no_match 為 exit 0。`prepare` 的 entry 指向 `/answer/channel-knowledge`，
沿用精確 Record/revision/content 用途政策，保存準備好的完整 Prompt 後回
`held / answer_generation_not_enabled`、exit 3。這表示鏈路到達明確的 LLM 邊界，
不表示產生回答、送出 messages 或呼叫真實 LLM。其他 held 為 exit 1，輸入/設定/IO錯誤為 exit 2。

本機輸出是 `entry-result.json` 與 `receipt.json`；沒有文字輸出 adapter 或公開發送。
公開 library `execute(..., mode=...)` 也只接受字串 `evidence` 或 `prepare`；其他型別或值
在讀取設定、輸入及建立目錄前以 `ValueError("offline_mode_invalid")` 拒絕，與 CLI 的 choices 一致。
收據連結同次輸入、入口選擇、原 Plan/task IDs、原 Result、collector、用途檢查與
`prepared-prompt.json`（若有）。各原生 stage 可選保存私有 stdin/stdout/stderr；
`ssr_chat.native_capture` 是只記錄位元組、再原樣執行原生 channel-search CLI 的 adapter，
不轉換 Query/Result，不用假後端代替實作。

每次執行固定五個 repo 的 HEAD/branch/dirty/source bytes，並核對前後來源及已列明的
合成設定/政策/snapshot fingerprint。新收據目錄採 exclusive create，不覆寫既有證據。
原始 streams 只適用於這個已授權隔離合成範圍，不應把秘密或未准真實資料傳給它。

這個程序不開 HTTP/socket、不登入、不使用 Registry/CIM、不開瀏覽器，不處理 manual-links，
也不重試既有拒絕。既有模型連線、真實來源、完整上下文、資料匯入/存放交接及網頁驗收
仍各自保留原阻塞。import/store 的 Evidence 不能直接當完整 Record 餵給 reader；
這裡直接讀原生 reader 的既有合成 Record fixture，沒有靜默補 ACL 或改 schema。
