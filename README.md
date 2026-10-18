# ssr-chat

## 本機 Ollama 模型 adapter

正式整合前可在本機用 Ollama 試跑模型；內部仍透過可替換的 `ModelAdapter` 介面呼叫。預設 adapter 是離線 fixture，只有指定 `--model-adapter ollama` 才會呼叫 Ollama。

連線預設為 `http://127.0.0.1:11434`。可用 `SSR_CHAT_OLLAMA_ENDPOINT`、`SSR_CHAT_OLLAMA_MODEL` 設定端點與已安裝模型，或在命令列傳入 `--ollama-endpoint`、`--ollama-model`。端點只接受 loopback HTTP 網址，prompt 不會送往遠端主機。

先列出本機已安裝的模型，再用合成問題驗證模型是否能依據提供材料回答；這些命令不會下載模型：

```powershell
python -m ssr_chat models
python -m ssr_chat verify-ollama --ollama-model <已安裝模型名稱>
```

用模型處理 fixture 事件時，指定 adapter 和模型：

```powershell
python -m ssr_chat chat --input fixtures/events/general.json --state-dir .local-state --model-adapter ollama --ollama-model <已安裝模型名稱>
```

模型只接收 runtime 組裝的使用者問題、同一份前文 snapshot、分析結果，以及各用途查詢回傳的素材和來源。模型請求失敗會記錄 `model_error`、不呼叫輸出 adapter，並交由固定 SOP；安全診斷只記錄錯誤代碼和通用說明，不記錄 prompt。需要 Ollama 已在本機啟動且指定模型已安裝；沒有 Ollama 或模型時，`verify-ollama` 會回報阻塞，不會自動安裝或下載。

本 repo 是獨立可執行的本機直播文字問答原型。只用 Python 標準函式庫與本 repo 的虛構 fixture；不連真實直播平台、模型、天氣或人物服務，也不依賴其他 repo。

## 功能與來源狀態

- 本機功能已完成：事件必要欄位／來源驗證、觸發與固定 SOP、前文 snapshot 回程、多用途規則拆分、各用途 query、Prompt、事件／trace 分開記錄、去重及模型／輸出失敗追蹤。
- Fixture／stub only：事件來源、天氣資料、人物／遊戲知識與模型使用本機 fixture；文字輸出為 dry-run。
- 真實 provider 尚待決定及接線：直播平台、正式模型、即時天氣。人物／遊戲素材方向已定為自有資料格式及人工整理的 Wiki／網路材料；目前沒有網路抓取 provider。
- 跨 repo 資料匯入、共用套件與 STT 明確延後，不是本 repo 本機驗收缺口。

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

## 查詢回傳與明確延後的 STT

五類查詢各自回傳 `status`、`materials`、`sources`；診斷細節只留在問答執行 trace。STT／逐字稿輸入與獨立 query 已明確延後，未來若重新排入，逐字稿需帶來源與時間範圍；一般逐字稿不觸發回覆。

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
