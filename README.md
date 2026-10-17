# ssr-chat

本 repo 是獨立可執行的本機直播文字問答原型。只用 Python 標準函式庫與本 repo 的虛構 fixture；不連真實直播平台、模型、天氣或人物服務，也不依賴其他 repo。

## 執行

uv 管理環境與 lockfile；不需安裝第三方套件。

```powershell
uv --cache-dir .uv-cache --offline lock
uv --cache-dir .uv-cache --offline run python -m ssr_chat chat --input fixtures/events/general.json --state-dir .local-state
```

同一個 state directory 會把前一個聊天室事件用在追問 snapshot：

```powershell
uv --cache-dir .uv-cache --offline run python -m ssr_chat chat --input fixtures/events/followup.json --state-dir .local-state
```

run trace、聊天室原文、SOP 與知識缺口分別寫入 .local-state 下的 JSONL 檔。輸出預設 dry-run，不會送到聊天室。

## 查詢回傳與預留 STT

五類查詢各自回傳 `status`、`materials`、`sources`；診斷細節只留在問答執行 trace。STT 目前未實作。未來主播逐字稿需帶來源與時間範圍，獨立查詢可回答「剛剛說了什麼」；一般逐字稿不觸發回覆，明確喚醒詞規則待決定。

## 本機 API

```powershell
uv --cache-dir .uv-cache --offline run python -m ssr_chat serve --port 8765 --state-dir .local-state
```

API 僅綁定 127.0.0.1。GET /health 回傳本機狀態；POST /chat 接收與 CLI 相同形狀的 JSON 事件。例：

```json
{
  "message_id": "api-001",
  "conversation_id": "demo-room",
  "author_id": "viewer-1",
  "source": "local-fixture",
  "text": "!ask 星界遠征是什麼遊戲？"
}
```

## 獨立測試

```powershell
uv --cache-dir .uv-cache --offline run python -m unittest discover -s tests -v
```

fixture 中的角色、遊戲、道具、攻略與天氣數值都是虛構測試資料。沒有資料時回傳明確的 no_data；工具故障回傳 tool_error。兩者不混為知識缺口，部分失敗時只使用已取得的素材。

## 本地延伸能力

- 原始聊天室事件與執行 trace 分開保存。有效事件以 source、conversation_id、message_id 去重；重啟後會從本地事件記錄重建索引，重複事件記錄後交固定 SOP。
- message_id 是穩定事件鍵的必要欄位；缺漏事件記入原始事件並交固定 SOP，不再生成隨機替代 ID。多需求分析以本機關鍵字和明確分隔符產生各用途 query text；同一含多用途的模糊子句可能分送到多個用途，這是規則型原型的限制。
- 前文按 conversation_id 隔離。知識 reader 要求素材標明 publication_status=published 並帶非空版本；候選素材不會當作無資料或知識缺口回覆。
- 模型失敗會記錄 run_status=model_error，answer 為 null，輸出狀態為 not_attempted，並交固定 SOP。輸出失敗亦記錄 trace 和 SOP；無法判定平台是否收到時，delivery_state 為 unknown，不自動重送。
- ModelAdapter、TextOutputAdapter、WeatherAdapter 與 KnowledgeReaderAdapter 僅是本 repo 的 Python 介面契約，沒有接通真實模型、平台或資料供應商。預設輸出仍是 dry-run。
