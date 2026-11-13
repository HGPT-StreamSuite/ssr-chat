# ssr-chat

## 獨立本機驗收網頁

此 repo 現有獨立網頁與可驗證標準見 [INDEPENDENT_ACCEPTANCE.md](docs/INDEPENDENT_ACCEPTANCE.md)。
它使用標準函式庫後端與靜態 HTML/JS，預設接到[固定原生入口](docs/NATIVE_REVIEW_WEB.md)，
實際顯示合成證據、用途拒絕與同次 prepared prompt，明確停在 LLM 邊界。可另選舊 Fixture 事件。
不連正式資料、Twitch 或 Ollama；G01 的一般聊天 intent 映射只供閱讀，仍為 blocked。

```powershell
$env:UV_PROJECT_ENVIRONMENT = 'C:\Projects\StreamSuite\runtime\envs\development-live-lifecycle'
uv --cache-dir C:\Projects\StreamSuite\runtime\uv-cache --offline run --no-sync python -B -X utf8 -m ssr_chat.review_web --port 8826
```

先確認 8826 未占用。啟動成功後開啟 `http://127.0.0.1:8826/`，在同一終端按 Ctrl+C 停止。
未啟動時該網址不可用；不自動切換埠。只建立／清理本次新建的合成暫存。
新增功能的程式與 UI 驗收紀錄另存於 `docs/evidence/review-web-acceptance.txt`；舊測試不代替網頁驗證。
全部 CLI/API/library 入口、格式、重送／修訂語意與待協調接口見 [SYSTEM_INTERFACES.md](docs/SYSTEM_INTERFACES.md)。

## 明確配置的模組化檢索入口

`retrieve` 以三個獨立子程序串接既有 QueryPlan、dispatcher 及 evidence collector；dispatcher 再呼叫配置的 native Query→Result CLI。只支援顯式 1.0.1、本機 CLI adapters，不匯入其他 repo、不改共用契約、不啟動服務、不生成回答或公開發送；目前 `chat`／`serve` 仍沿用原來的問答路徑。

```powershell
uv --offline run --no-sync python -m ssr_chat retrieve --request <request-and-trusted-context.json> --pipeline-config <trusted-local-pipeline.json> --output <new-result.json>
```

pipeline JSON 欄位為 `schema_version: "1.0.1"`、既有 `python` 執行檔、`planner_repo`、`dispatcher_repo`、`collector_repo`、`dispatcher_config` 路徑及可選 `timeout_seconds`（1–120，預設 60）。相對路徑以配置檔目錄為準；各 dispatcher adapter timeout 必須較短。dispatcher 設定沿用其原本 access_scope／adapters 契約，只接受 kind=cli，不替任何來源增加權限。配置與 trusted_context 由可信 host／操作者提供，不從問題文字產生；可信 CLI 是可執行程式碼，此入口不是程式沙箱。

結果保留原 Plan、各 task 的原 Result 與 collector 回傳，按 task_id 精確映射；部分失敗回 incomplete／exit 1，無效配置或介面錯誤回 exit 2。缺設定不回退 fixture、不把錯誤當空結果、不合併不同 task 的同名 domain。輸出採 exclusive create，既有檔案保留；結果可能含授權原文，應留在受保護本機。stages 記錄實際 argv/cwd、耗時、退出碼及輸出雜湊；不保存子程序 stderr 本文。這個入口不代表 21 項功能已整合或有真實來源／LLM 的端到端驗收。

### 選用：常駐 worker 執行模式

預設每個階段（planner、dispatcher、collector，以及 dispatcher 呼叫的讀取元件）每題都新開一個 Python 程序，讀取元件的嵌入模型也因此每題重新載入。pipeline JSON 加上 `"execution": "warm_workers"` 後，同樣的進入點改在常駐的 Python 程序裡重複執行（`ssr_chat/warm_pool.py`、`warm_worker.py`）；省略或設為 `"subprocess"` 則維持原行為。

- 每個不同的進入點（直譯器、script 或 `-m` 模組、cwd）各有一個 worker 程序，用 `runpy` 執行原本的檔案，argv、stdin、stdout、stderr、結束碼語意與一般程序相同，只有元件匯入模組內的狀態（例如已載入的嵌入模型）會留存；各元件的 `hgpt_ssr` 套件因此不會互相覆蓋。實測與一般子程序的 planner／collector 輸出逐 byte 相同。
- dispatcher 的 worker 會把 `kind=cli` 的讀取元件 adapter 也交給常駐 worker，錯誤碼沿用原本的 `adapter_timeout`、`adapter_output_limit`、`adapter_exit_nonzero`、`adapter_start_failed`；adapter 的 timeout、輸出上限、`access_scope` 仍由 dispatcher 原本的設定與檢查決定。
- 只認得 `python [-B] [-X utf8] (script.py | -m 模組) 參數…` 這種命令；其他形式（`uv run …`、`-I`、`-c`、非 Python 程式、`native_capture` 這類包裝內部再開的程序）一律當一般子程序執行，不會失敗也不會加速。
- 逾時或 worker 異常結束時，該 worker 會被終止，下一題自動重啟，因此重啟後的第一題要重新載入模型。同一個 worker 一次只處理一題（同一進入點的請求排隊）。
- 常駐程序會保留元件的模組層快取（含嵌入向量快取）。要清掉就呼叫 `ModularRetrievalPipeline.close()` 或重啟本程序；本程序結束時 worker 也會跟著結束。
- trace 的 stages 在此模式下多一個 `"execution": "warm_workers"` 欄位；dispatch 的 `pid` 是實際執行讀取元件的 worker 程序。

### 本機 API 的獨立檢索路徑

`serve` 可另明確指定 `--retrieval-pipeline-config <pipeline.json> --retrieval-template <server-template.json>`，啟用 POST `/retrieve`。兩檔皆由可信本機啟動配置提供；template 使用與 `retrieve --request` 相同的 request/trusted_context 封套，固定 scope、ACL、layer、limit 及來源上下文。HTTP body 只接受 `{"request_id":"local-001","text":"問題"}`；額外欄位、重複 JSON keys 或非有限數值會拒絕，不能從 HTTP 指定權限、模板或元件命令。未配置回 503/not_configured，不回退 fixture。

這是與 `/chat` 分開的證據檢索路徑，**不把檢索證據交給回答模型、不呼叫文字輸出**。search/可見性授權不能取代既有 ai_input 授權，後續模型接線仍須另保留來源用途閘門。成功或 partial 皆以 HTTP 200 回傳完整狀態，呼叫端必須檢查 status 與 collection.partial；元件故障回 502，無效請求回 400。`/health` 的 modular_retrieval 顯示配置與資料模式宣告，不代表來源或端到端已通過驗收。原 `/chat` adapter、dry-run 及既有資料庫路徑保持原配置；不因加入此端點就接通 Twitch。

多個資料域可另用 `serve --retrieval-routes-config <trusted-routes.json>` 配置獨立端點，例如 `/retrieve/chat-history`。配置固定為 `{"schema_version":"1.0.1","routes":{"/retrieve/chat-history":{"domain":"chat_history","pipeline_config":"chat-pipeline.json","template":"chat-template.json"}}}`；檔案路徑以 routes 配置目錄為準。每條 route 要求模板的 request.scope.domain 相符、caller read selectors 僅屬該 domain、write selectors 為空；不會合併不同 route 的權限。HTTP body 仍只能傳 request_id/text，未知 route 回 not_configured，不能指定 profile、scope、帳號、場次或路徑。`/health` 的 modular_retrieval_routes 只列配置描述。

近期打字查詢的帳號、頻道、場次、目前事件 ID／時間由可信 host 模板提供，planner 與 chat reader 各自保留排除目前事件及嚴格時間上界。模糊「剛剛說什麼」在既有規則可能指向 STT；明確打字問題才用聊天查詢，不把語音與聊天室混成同一來源。本機合成測試不代表已接 Twitch 事件或真實觀眾資料。

目前交付的 planner 對近期打字問題輸出 `recent_utterance`，chat reader 的相同用途為 `last_said`。只有在該 route 的 pipeline JSON 明確設定 `"chat_recent_intent_adapter":"recent_utterance_to_last_said_v1"` 才轉接此名稱；限 1.0.1、task/query 都為 chat_history，不變更其他欄位、ACL、時間界線或其他 intent。回傳保留原始 `plan`、實際送入 dispatcher 的 `dispatch_plan` 與 `query_adaptations`；collector 仍以原始 plan 核對證據。未配置時維持原生 Result／失敗，不自動猜測或修補未知契約。這個轉接不改動各獨立 repo。

### 逐字稿查詢的本機接線

`/retrieve/transcripts` 可配置為 domain=`stt_transcript`，以同樣的 planner／dispatcher／collector 流程呼叫 task-13 的 `python -B -X utf8 -m source.hgpt_ssr --snapshot <trusted-snapshot.json> --trusted-context <reader-grants.json> --query -`，cwd 固定為該候選目錄。stdout 是原生 1.0.1 Result，成功／no_match 為 exit 0，其餘為 exit 2；主機器人保留 denied、blocked_by、缺少輸入與 partial，不轉成空白成功。具名路由現在會在 dispatcher 執行前核對每個 task 及 query.scope 的資料域，並在 collector 前核對 Result.domain。

`fixtures/retrieval/transcript-raw.template.json` 與 `transcript-published.template.json` 是明確標為 synthetic 的 host 模板範例，固定測試帳號、頻道、場次、版本、layer 與 read scope；沒有真實授權。請在可信 routes 配置中指定相應 pipeline／template 路徑。HTTP 仍只接受 request_id/text，不能指定錄音、快照、來源政策、歷史修訂或權限。原生 reader 的授權設定、caller ACL 及 Record ACL 都須通過，不由模板或文字取代。

目前接入的是指定範圍的 current 修訂查詢；raw 可保留 partial／final 辨識狀態，published 需通過原始引用檢查。reader 原生依時間正序套 limit，本入口不宣稱已支援「最近一句」選取；範例未提供 current_event，近期語音問句會保留缺少事件界線的狀態。未接麥克風、STT 引擎、Twitch 或回答模型，未切換既有常駐 API。

### 頻道知識查詢的本機接線

`/retrieve/channel-knowledge` 可配置為 domain=`channel_knowledge`，沿用 planner／dispatcher／collector，透過 task-10 的 `python -B -X utf8 source/ssr.py --config <trusted-reader-config.json> --request -` 取得原生 1.0.1 Result；cwd 固定為候選目錄。不轉換 Query／Result，也不把非零退出碼當成空查詢：missing_input=2、not_configured=3、denied=4、error=5 的原生 Result 與退出碼保留於 dispatcher。collector 另外依自身契約回傳彙整 status／partial；例如原生 not_configured 可彙整為 missing_input，兩層狀態需分開檢查。reader 專屬的進階條件也可能被 collector 拒絕，不能視為已支援。

`fixtures/retrieval/channel-raw.template.json` 與 `channel-published.template.json` 為合成 host 範例，固定頻道、場次、內容版本、layer 與 read scope，使用空 `conditions`，沿用已核對的 reader 1.1.0 預設詞項搜尋（terms）。collector 目前不接受 `text_match` 條件，故不顯式傳入這個冗餘預設；其他搜尋模式尚未接入。完整 HTTP 問句原樣送入原生 reader，例如「請查詢本頻道 launch」；不在主機器人抽取關鍵字、改寫問題或呼叫模型。caller ACL、native config 的 scope/layer grants 與 Record ACL 分別檢查；搜尋時使用目前修訂，最新修訂被拒時不回退舊修訂，撤回事件也不因文字命中而恢復。

具名路由的 `/health` 現在同時列 domain 與可信配置的 layer（未指定時依 planner 預設為 raw），不包含帳號、scope、權限、條件或檔案路徑；這些是配置標記，不能代替實際來源驗證。無效 layer 於載入時拒絕。raw／published 使用獨立 route，HTTP body 仍只有 request_id/text，不能變更 layer、歷史查詢、來源或授權。此入口僅回傳證據，未把資料送入回答模型或 Twitch，未切換既有 8775 服務。

## 隔離的合成證據回答入口

`serve --answer-routes-config <trusted-answer-routes.json> --model-adapter ollama --ollama-endpoint <loopback> --ollama-model <installed-model>` 可另啟用 `/answer/channel-knowledge`。設定形狀為 `{"schema_version":"1.0.1","routes":{"/answer/channel-knowledge":{"retrieval_route":"/retrieve/channel-knowledge","purpose_policy":"channel-synthetic-ai-policy.json"}}}`；只引用已明確配置的具名檢索路由。HTTP 仍只接受 request_id/text，不能送入權限、政策、模型或檔案參數。現有 `/chat` 與 `/retrieve` 的行為保持獨立。

回答切片只接受明確標為 synthetic 的 raw 頻道、受限近期聊天或固定時間窗單一逐字稿片段，且檢索必須完整成功。`fixtures/retrieval/channel-synthetic-ai-policy.json` 是頻道合成測試用途許可範例：每筆許可固定完整 scope、source_id、record_id、revision、layer、source_kind 及內容 SHA-256，ai_input 必須為 allowed 且有明確 basis。查詢可讀、synthetic 標籤或已發布狀態都不能自動授予 AI 輸入用途；缺少／unknown／prohibited 許可、內容變更、非本筆 raw 自我來源引用都停止生成。這不是正式來源的授權設定。

允許的原文、修訂、事件時間、scope、quality flags 與 SourceRef 以 E1/E2 等標籤交給既有 ModelAdapter；Ollama 只在這個明確提示格式加入固定引用要求，一般聊天提示不變。回答必須引用提供的識別碼，未知或缺少引用會暫扣；引用檢查不等於逐句事實驗證。沒有 TextOutputAdapter 或 Twitch 發送步驟，回答及完整檢索結果只在本機 API 回傳。

`state-dir/modular_answer_traces.jsonl` 會在模型呼叫前保存通過用途檢查的 prompt、政策雜湊及 Ollama request body，完成時保存 adapter 回答與引用結果；原始 Ollama thinking／未經既有措辭約束的 content 不在此介面暴露。trace 寫入失敗會停止這次流程。此 trace 可能包含允許的原文，只留在受保護本機。這個端點尚未載入既有 8775，未接真實觀眾資料或正式來源。

`/answer/chat-history` 可另引用既有 `/retrieve/chat-history`，配合 `fixtures/retrieval/chat-synthetic-ai-policy.json`。聊天許可另外綁定 actor_account_id，內容雜湊與模型素材也保留此帳號。主機器人再次檢查可信 host 的同帳號、完整 scope、單一場次、目前事件排除、嚴格 event_time 上界及 limit=1；只接受既有規劃器的近期打字查詢，不宣稱支援一般歷史或跨帳號問答。時間界線沿用 native 的 event_time 語義，不是 received_at 的歷史可用性快照。

聊天回答使用固定的結構化原句引用契約：模型只回 JSON 物件 `{"quote":"原句","evidence_id":"E1"}`。嚴格解碼後，quote 必須與來源原文完全相等（包括原有字面跳脫與實際換行），且引用必須有效；格式錯誤、改寫或遺失原句會 held。API 的 quoted_evidence 保存驗證後的模型欄位，answer 僅由這個 quote 加上引用標籤格式化，不補入模型未回傳的原句。原始 adapter JSON 回覆、解碼原句及來源檢索結果、actor、事件時間、修訂與 SourceRef 分別保留在本機 trace。沒有 policy、權限為 unknown/prohibited 或帳號／範圍／內容不符都不呼叫模型。來源合成標記本身不構成用途許可；目前尚未接真實 Twitch 身分與真實聊天授權。

### 固定時間窗逐字稿回答

逐字稿回答可另配置 `/answer/stt-transcript`，引用 domain=`stt_transcript` 的合成 raw route，使用 `fixtures/retrieval/stt-answer.template.json` 與 `stt-synthetic-ai-policy.json`。此切片要求可信 host 固定帳號、完整場次／版本 scope、明確 time_from/time_to、limit=1、host request 的 conditions={}（上游規劃後只接受 stt_lookup 與 revision_view=current），以及完整成功的檢索結果。只接受 current 有效修訂及 final 狀態，整段 started_at/ended_at 必須在時間窗內；partial、失效修訂、跨窗或不完整來源皆停止生成，不宣稱「最近一句」。

用途許可與內容雜湊另外綁定 actor_account_id 及完整 extensions.stt，包含段落識別碼、修訂、辨識狀態、speaker_id、時間及 source_confidence。原始 metadata 與 quality_flags 會保留；信心值不轉成真實性機率，speaker_id 不自行推成 Twitch 身分。模型仍只回 quote/evidence_id JSON，嚴格比對原句後，API 加上「逐字稿辨識文字」標示，recognition 欄位明示未對照錄音驗證。這是文字快照讀取與回答，沒有錄音或 STT 引擎。

正向測試另行撰寫獨立 scope 的合成逐字稿與明確用途許可，沒有刪除原快照的被拒來源或改動原授權；原完整快照仍以 partial 控制案例維持 fail closed。既有 8775 尚未載入此路徑，未接實際觀眾或真實語音。

目前已驗證的自身查詢問句為「查詢我的逐字稿」。「讀出我的逐字稿」仍會觸發上游規劃器的身分條件缺失，保留為模型呼叫前停止的案例；「我剛剛說什麼？」亦未接入，不能當作本切片已支援的最近語音查詢。

### 本機統一查詢入口

`serve --entry-routes-config <設定檔>` 可額外啟用 `POST /query`。設定格式見 `fixtures/retrieval/entry-routes.example.json`，其中每個 target 都必須先由 retrieval/answer routes 設定建立；範例僅提供映射，啟動時不會自動建立缺少的來源或放寬權限。

HTTP 仍只收 `request_id` 與 `text`。主入口支援四個明確文字命令：`/game <遊戲問句>`、`/channel <頻道問句>`、`/history <聊天問句>`、`/transcript <逐字稿問句>`。命令只選擇主機預先綁定的獨立 gateway；來源、actor、scope、session、layer、時間窗、讀取權限與 AI 用途許可持續由原路徑決定。移除命令前綴後，完整問句交給該路徑；原始輸入與實際轉交內容均記錄於本機 `modular_entry_traces.jsonl`，選路紀錄會先於檢索或模型呼叫寫入。

沒有命令、未知命令、多個命令、未配置目標或下游失敗均停止，沒有跨路徑重試或舊聊天 fallback。各路徑的自然語言能力與限制原樣保留；遊戲預設映射只回檢索證據，不自動授予模型輸入用途。主入口目前只允許宣告為合成資料、禁止公開輸出的 domain-bound 路徑；回答 target 仍要求 raw 與引用檢查。這不是任意自然語言意圖分類器，也沒有串接真實 Twitch。

回應以 `command/domain/operation` 說明選路，`target_result` 保留下游完整結果。`evidence_ready`、`no_match`、`answered`、`held` 或 `model_error` 區分實際結果；若回答 target 意外拋出例外，`model_invoked=null` 表示無法確認是否已呼叫模型。健康檢查只列命令、domain、operation 與 layer，不顯示權限、身分、來源路徑或 policy。未指定設定時 `/query` 回 503；既有 `/chat` 維持原入口協定。常駐 8775 未重啟，因此沒有載入這個新入口。

### 可選的自然問句分流

將入口設定的 `routing` 明確改為 `explicit_or_planner_v1` 可接入 task-14 的規則分類／QueryPlanner；格式見 `fixtures/retrieval/natural-entry-routes.example.json`。原有四個文字命令仍優先處理，未知或格式錯誤的 slash 命令不會改走自然分類。舊 `explicit_command_v1` 設定仍只接受命令。

自然問句先做單句檢查，再分別使用各路徑自己的主機 template 呼叫 task-14 CLI，僅清除 request.scope.domain 這個選路提示。每次只產生 Plan，沒有執行 dispatcher、讀取 domain 資料或呼叫模型；各路徑的 actor、scope、session、時間窗、conditions 與讀 ACL 不合併。只有一個 domain 有分類訊號，且其 Plan 完整、只有一個無依賴 task、沒有 missing/denied 或其他 domain 訊號時才可選路。選中後仍以完整原問句重新走該路徑的原有檢索與用途許可，不直接執行分類 Plan。

這個入口不把規則分類視為任意自然語言理解。複合問句、未知分類、歧義、缺資料、分類器失敗或缺權限均 held。聊天自然問句另限明確自稱的最近打字形狀，例如「我剛剛在聊天頻道打了什麼訊息？」；逐字稿限「查詢我的逐字稿」等明確自己的固定時間窗問句。第三人稱、任意名字或其他未確認措辭不自動推成主機帳號；最近語音仍未接入。裸道具名與缺少明確領域詞的頻道問句可能沒有分類訊號，仍可使用既有命令。

完整分類 Plan 與原始 stage 紀錄只進本機 entry trace；HTTP 僅回分類狀態、候選 domain 與停止原因，不公開其他路徑的身分／ACL。分類不產生 AI 用途授權。遊戲仍是證據檢索；完整自然問句未命中搜尋時回 no_match，不自動抽取或改寫道具名。既有 8775 未切換或重啟，沒有載入此設定。

### 登記實體的搜尋文字抽取

自然入口可再明確設定 `query_extraction`；格式見 `fixtures/retrieval/extraction-entry-routes.example.json`。目前只有兩個固定句型：`請找出 <已登記實體> gameplay records` 與 `請問本頻道公告 <已登記主題> 幾點？`。每條規則綁定已配置路徑的完整 scope，登記名稱只允許解析，不代表資料存在或具有讀取／AI 用途許可。未登記、未知句型或存在第二個登記解讀時，會在資料讀取前 held；不選第一個或最長的名稱。原命令路由不抽取。

原問句繼續用於分類、入口 trace、模型 user_text 與 analysis。搜尋文字則是原問句中已登記名稱的完整原始子字串，記錄字元 span、規則、entity ID、scope SHA-256 與 registry SHA-256。若名稱本身含 gameplay 或「公告」，該詞仍保留在搜尋文字中。登記重疊名稱可能讓領域詞也成為實際名稱的一部分；此時停止，要求更明確的問句，不自動刪詞。

搜尋器另以抽取文字與原主機 template 產生真實 Search QueryPlan，再依原契約送 dispatcher 和 collector；不竄改既有 Plan、Query/Result schema 或 Evidence。wrapper 的 `original_question`、`query_extraction` 與入口保存的分類 Plan 串起原問句和 Search Plan。scope、actor、ACL、layer、limit、時間窗、conditions 與來源／用途政策均沿用原路徑。no_match、來源錯誤及拒絕狀態原樣傳遞；空搜尋不會被改成成功。

這是有限登記詞彙與固定文法，沒有開放式實體推測、同義翻譯或任意停用詞刪除。既有配置沒有 `query_extraction` 時維持原搜尋行為。8775 仍未重啟，未載入新抽取設定。

## 用途政策授權產生工具

回答路徑只讓用途政策中 `ai_input=allowed`、且內容雜湊相符的記錄進 prompt。`ssr_chat.purpose_grants` 用勾選表產生這份政策，不必手寫 JSON：

```powershell
python -B -X utf8 -m ssr_chat.purpose_grants list  --reader <reader.json> [--policy <現有政策.json>] --out <勾選表.md>
python -B -X utf8 -m ssr_chat.purpose_grants apply --review <勾選表.md> --policy <政策.json>
python -B -X utf8 -m ssr_chat.purpose_grants check --reader <reader.json> --policy <政策.json>
```

`reader.json` 指定一個 raw 範圍與它的原生讀取元件：`{"domain","command","cwd","scope"}`，`command` 從 stdin 讀 1.0.1 Query（例如 channel-search 的 `source/ssr.py --config <cfg> --request -`）。`list` 以空白查詢瀏覽全部記錄並依 `source_event_id` 分頁，預設全部不勾；已有效授權的記錄預先勾選，內容已改變的標示失效且不勾。`apply` 只把打勾的記錄寫成授權，以 `PurposePolicy` 驗證後覆寫，舊檔備份為 `*.bak-<時間>`；最多 200 筆。`check` 有失效授權或記錄已不存在時結束碼 1。勾選表與 `*.records.json` 可能含真實內容，請放在不進 git 的位置。目前只有 `ai_input` 一種用途；本機模型／雲端 API 分開授權尚未實作。

## 已發布知識庫唯讀串接

`chat` 與 `serve` 可明確選擇 `--knowledge-adapter catalog`，透過另一個程序呼叫既有 `ssr-knowledge catalog search --names-only`。必須指定現有 checkout、Python 與 DB 資料夾，不安裝依賴、不匯入其他 repo、不建立 DB、不開啟 preview，也不在失敗時回退 fixture。未選擇時維持原有 fixture 模式。

最小可驗證入口為 `!ask 「每日地圖輪替」的規則是什麼？`：用引號內的單一完整名稱／別名查詢；也接受只有名稱的問題。未加引號的單一道具效果問句支援 `X是什麼效果？`、`X有什麼效果？`、`X的效果是什麼？`（X 與問法間可留空格），由 catalog reader 將明確句尾移除，再以完整 X 做 names-only 查詢；原始問題與分析內容仍保留給 Prompt。這是有限句型處理，沒有名稱清單、模糊搜尋或模型抽取。reader 收到的查詢若含標點分句、和／與／以及／還有等多主體文字，不做這項抽取；名稱本身含這些文字時請加引號。這不擴充 analyzer 的分句能力，不支援多道具比較。明確代名詞追問（例如 `!ask 它的重置時間呢？`）可引用同聊天室上一個帶引號的提問，僅支援一層追問。未命中不猜關鍵字、不從舊問題補入另一個主題；同名多義需要重新指定名稱。任意自然語句的實體辨識、多主題／多層追問仍未實作。

```powershell
uv --offline run --no-sync python -m ssr_chat chat --input <合成事件.json> --state-dir <隔離測試狀態目錄> --knowledge-adapter catalog --knowledge-repo <ssr-knowledge完整路徑> --knowledge-python <既有Python完整路徑> --knowledge-data-dir <正式資料目錄> --model-adapter ollama --ollama-endpoint http://127.0.0.1:11435 --ollama-model qwen3.5:9b-q4_K_M
```

reader 沿用 catalog 的已發布、未過期、來源查詢權限與完整性檢查。送進模型前，所有引用來源的歷史與目前 `ai_input` 都必須為 `allowed`；`unknown`／`prohibited` 會排除整筆混合來源內容並記錄 `catalog_ai_input_not_allowed`。已發布不等於已授權 AI 使用，不修改正式來源政策。送入素材保留修訂、來源、限制、已知未知與結構欄位；不帶編輯／審核者、原文快照及來源設定。

工具失敗保持既有行為：以 `tool_error` 且無該工具素材組裝 prompt，模型可回覆資訊不足，最終仍僅 dry-run；不新增「所有工具失敗便禁止回覆」的產品規則。模型失敗依原邏輯不呼叫輸出。完整問題與 messages 的驗收證據只保存於受限本機產物；公開直播接收與發送尚未驗收。

## CLI 結果與 9B 驗收

`chat` 將無效事件、未觸發事件及重複事件記錄並交由固定 SOP；這些正常的 `held` 結果仍以 exit code 0 表示已處理。模型失敗與輸出失敗會分別回傳 `failed`、`delivery_failed`，並以非零 exit code 表示執行未完成。

`verify-ollama` 會確認模型回答明確肯定素材中的「30 點能量」數值，並拒絕其他數值、否定句及像「300 點」這類包含 30 子字串的答案。`run-9b` 每次建立新的 fixture message ID、conversation ID 與 gitignored state，並核對回答、trace 及 dry-run 狀態；成功不代表已連接真實平台或送出公開回覆。這個流程只驗證已安裝的 `qwen3.5:9b-q4_K_M`；缺少模型時會停止，需另外明確執行 `pull`。

## 本機 Ollama 模型 adapter

回答中的未知不能推成「官方尚未公開」。Ollama adapter 除 prompt 指示外，會將含否定公開／公布／披露措辭的整行保守替換為「目前提供的資料不足，無法確認。」，其他行原樣保留。這個一般性約束沒有 PEAK 答案或數值，也不推斷外部官方網站的發布狀態；它可能捨棄同一行的其他資訊。它是有限措辭防護，不能保證辨識所有同義改寫或取代事實查核。原始模型回應與 adapter 約束後答案在私有驗收證據中分開記錄，不能宣稱兩者逐字相同。


本 repo 對 chat API 明確送出 `think: false`。本機 Ollama v0.40.6 回報 `qwen3.5:9b-q4_K_M` 預設 thinking 為 true；省略此欄時，合成 fixture 曾生成 3,768 tokens thinking 並耗盡 4,096-token runner context，`message.content` 為空。相同 fixture 明確關閉 thinking 後回覆長 36 字元、`done_reason=stop`、`eval_count=24`，CLI 回 `answered`。adapter 只使用 `message.content`，不會把 `message.thinking` 當答案；目前不覆寫 `num_predict` 或 `num_ctx`。此設定適用聊天室最終回答，其他需要 thinking 的工作模式尚未實作。

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

本 repo 是獨立可執行的本機直播文字問答原型。只用 Python 標準函式庫；預設使用本 repo 的虛構 fixture，可明確啟用本機 Ollama 與 knowledge catalog CLI。尚不連真實直播平台或天氣服務。

## 功能與來源狀態

- 本機功能已完成：事件必要欄位／來源驗證、觸發與固定 SOP、前文 snapshot 回程、多用途規則拆分、各用途 query、Prompt、事件／trace 分開記錄、去重及模型／輸出失敗追蹤。
- Fixture／stub：事件來源與天氣資料；模型與知識預設 fixture，可選本機真實 adapter。文字輸出為 dry-run。
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

API 僅綁定 127.0.0.1。GET /health 回傳本機狀態與 configured_adapters（knowledge/model/weather/output）；全預設才顯示 local_fixture，其餘顯示 local_adapters。這些是配置標記，不代表已實測 provider 連線或回答正確，且不包含模型名稱、端點或資料路徑。POST /chat 接收與 CLI 相同形狀的 JSON 事件。例：

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
- ModelAdapter 與 KnowledgeReaderAdapter 已有可選本機實作；TextOutputAdapter 維持 dry-run，WeatherAdapter 維持 fixture。公開平台輸出尚未接線。

## 專案自管 Ollama runtime
受管理預設模型固定為 qwen3.5:9b-q4_K_M（Q4_K_M 量化，官方標示 6.6GB）。原有的 qwen3.5:4b-q4_K_M 保留在專案私有 .runtime/ollama/models 中；pull 僅安裝指定 9B 標籤，不會覆寫或刪除既有模型。

Windows 可由本 repo 啟動官方 standalone runtime。執行檔、模型與 logs 放在 Git 忽略的 .runtime/ollama/。不會修改全域 PATH、使用者環境變數、登入啟動、Windows 服務或防火牆。server 僅綁定 127.0.0.1:11435。啟動時若 port 被占用，管理器會停止並回報，不會連接或終止該程序；停止時會比對 owner record 的 PID、啟動時間與執行檔完整路徑，只停止本 repo 啟動的 server。

目前鎖定 Ollama v0.40.6 官方 standalone ollama-windows-amd64.zip，SHA-256：e359637575302f288d76c0007c408a4be9c7376d59c4f28684d63b232a44b427。本機下載檔為 1,471,164,488 bytes（1.37 GiB）；解壓後為 1,942,921,587 bytes。ZIP 內含 Ollama CLI 及 CUDA/Vulkan runtime DLL，沒有附第三方授權文字；Ollama 原始碼採 MIT，模型權重有自己的授權條款。本 repo 不重新散布 runtime 或模型。

已知 Ollama 預設 store 與舊專案設定路徑都沒有 qwen3.5:9b-q4_K_M manifest，因此未複製或搬移模型，也沒有刪除任何舊資料。首次 pull 會將官方模型頁列為 6.6GB 的模型放進 .runtime/ollama/models/；實際占用以本機 blob 大小為準。

Windows 命令（在 ssr-chat repo 根目錄執行）：

    .\scripts\ollama-runtime.ps1 start
    .\scripts\ollama-runtime.ps1 status
    .\scripts\ollama-runtime.ps1 pull
    .\scripts\ollama-runtime.ps1 models
    .\scripts\ollama-runtime.ps1 verify

使用 runtime 處理本 repo 的合成 fixture：

    uv --cache-dir .uv-cache --offline run python -m ssr_chat chat --input fixtures/events/general.json --state-dir .local-state-ollama --model-adapter ollama --ollama-endpoint http://127.0.0.1:11435 --ollama-model qwen3.5:9b-q4_K_M

結束後執行 .\scripts\ollama-runtime.ps1 stop 停止專案啟動的 server。models、pull 與 verify 只會呼叫已確認由本 repo 啟動且健康的 endpoint。
補充：一般 `start` 會持續等待 server 結束；`start -ReturnAfterHealthy` 可在 health 成功後返回，供同一 PowerShell 工作階段執行 `status`、`models`、`verify`、`stop`。若要讓 9B 的完整啟動、health、verify、fixture 問答與 finally stop 保留同一個 Process 物件，請使用：

```powershell
.\scripts\ollama-runtime.ps1 run-9b
```

`run-9b` 僅會停止本次啟動且仍由 Process 物件驗明身分的子程序。舊 owner record 只有在同一執行確認原程序已不存在或 PID 已重用，且 11435 沒有 listener 時，才會改名封存為 `server-owner.stale-*.json`；無法證明時會保留記錄並停止。

PowerShell lifecycle 測試：

```powershell
.\tests\test_ollama_runtime_lifecycle.ps1
```

管理器只在專案子程序環境設定 `OLLAMA_NO_CLOUD=1`，停用 Ollama cloud 功能；官方模型 registry 下載仍由 pull 執行。

本機合成 Twitch 事件轉接與限制請見 [docs/TWITCH_REPLAY.md](docs/TWITCH_REPLAY.md)。

合成事件正規化到獨立儲存的 CLI 請見 [docs/CHAT_STORAGE.md](docs/CHAT_STORAGE.md)。

Synthetic Twitch replay to isolated task-20/task-7 storage: [docs/TWITCH_ARCHIVE.md](docs/TWITCH_ARCHIVE.md).

Synthetic STT text to isolated task-21/task-8 storage: [docs/STT_STORAGE.md](docs/STT_STORAGE.md).

Registered synthetic game snapshots to task-22/task-5 storage: [docs/GAME_STORAGE.md](docs/GAME_STORAGE.md).

Registered synthetic channel exports to task-19/task-4 storage: [docs/CHANNEL_STORAGE.md](docs/CHANNEL_STORAGE.md).

## Synthetic message counts

The independent `count-activity` CLI reads a pinned synthetic task-7 archive and runs task-18 with explicit speech marks. Counts preserve revision, source, scope and purpose boundaries. See [ACTIVITY_COUNTS.md](docs/ACTIVITY_COUNTS.md). This CLI is not loaded on the running 8775 API.

## Synthetic transcript cleanup

The independent `draft-transcript` CLI reads a pinned synthetic task-8 history and creates separate task-9 whitespace-only cleanup drafts with exact source and permission checks. See [TRANSCRIPT_DRAFTS.md](docs/TRANSCRIPT_DRAFTS.md). This CLI is not loaded on the running 8775 API.

## Synthetic knowledge quote drafts

The independent `draft-knowledge` CLI reads a pinned synthetic channel current view and runs native task-23 through an exact-bound host source policy callback. Explicit caller marks and Unicode fragment references remain traceable. See [KNOWLEDGE_DRAFTS.md](docs/KNOWLEDGE_DRAFTS.md). This CLI is not loaded on the running 8775 API.

## Synthetic channel events

The independent `channel-events` CLI connects declared synthetic relationship/visit events to native task-26 ingest/get/export in an owned SQLite ledger. See [CHANNEL_EVENTS.md](docs/CHANNEL_EVENTS.md). It does not infer visits or change manual person/account links, and is not loaded on 8775.

## Synthetic raw identity records

The independent `identity-records` CLI connects declared synthetic person/account raw records to native task-6 and owned storage. See [IDENTITY_RECORDS.md](docs/IDENTITY_RECORDS.md). It does not infer or create person-account links and is not loaded on 8775.
