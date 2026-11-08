Current synthetic G01 implementation: see docs/CHAT_HISTORY_BINDING.md and contracts/interfaces.v1.json. Only chat_history-to-history is authorized for this isolated profile; broader recent/LT and production decisions remain open.

# ssr-chat 系統流程與接口

本 repo 負責主機器人的本機 orchestration、可信設定綁定、入口選擇、用途政策與輸出邊界。
各資料域的 importer/store/reader、身分連結與 native Query/Result 的語意由對應 repo 負責。
目前可保存的完整原生證據到「允許的 prompt 已準備」；沒有真實 LLM 或公開發送驗收。

## 可機器讀取的契約

- `contracts/interfaces.v1.json`：15 個現存 CLI 命令、實作／測試來源、5 個主要 library 入口、HTTP 路由、輸入輸出、相依與交接。
- `contracts/review-native.v1.schema.json`：本機合成網頁的輸入 JSON Schema；未新增 validator 依賴。
- `fixtures/contracts/review-native.v1.cases.json`：6 個原生正向／業務拒絕／空資料／指令錯誤案例、5 個輸入拒絕案例、重送規則與預期結果。
- `tests/test_interface_contracts.py`：以實際公開驗證與 dispatch 核對上述契約、拒絕發生前沒有註冊原生工具、未配置 API 不回退聊天，以及 CLI 索引沒有漏列入口。

`ssr-chat.review-native.v1` 只標記本機網頁封套。既有 native `schema_version=1.0.1`、
QueryPlan、Result、Evidence、Record、legacy ChatRuntime event 仍是不同資料型別。
JSON Schema 不能代替當前 workspace、重送、ACL 或來源指紋檢查。

## 入口至出口

| 入口 | 真實流程／出口 | 信任與完成界線 |
| --- | --- | --- |
| `python -m ssr_chat chat`、POST `/chat` | EventValidator → event/context → trigger/analysis/queries → ModelAdapter → TextOutputAdapter；回 runtime 結果及本機 traces | 預設 Fixture/dry-run；與 native 檢索不同；配置描述不等於連線成功 |
| `retrieve`、POST `/retrieve/*` | host template → planner → 可選明示近期 intent adapter → dispatcher/native reader → collector | 1.0.1；保留 Plan/Result/task 對應、partial 與 exits；不送模型 |
| POST `/answer/*` | 命名 retrieval → 完整證據／原文引用檢查 → 精確用途政策 → prompt → 可配置 ModelAdapter | 搜尋可讀不等於 ai_input 許可；review 限 prepare_only，不啟用模型 |
| POST `/query` | EntryGateway 選一個已註冊的 command/domain route → retrieve 或 answer | 不混用多個 route 的 scope/identity；未知、巢狀或歧義保留 held |
| `python -m ssr_chat.local_pipeline`、`execute()` | 同一 EntryGateway + 5 個 canonical checkout → 原生 streams、receipt、可選 prepared-prompt | 僅隔離 synthetic；前後 source/config/data hashes 相同才可視為穩定證據 |
| review POST `/api/native/run` | 固定 tool → `run_native` → `NativeReview` → 上述真實 execute → 同次結果與私有原始收據 | 預設 native，Fixture 另選；不接受命令、路徑、Python、ACL、真資料或網路目的地 |
| 各 `ingest-*` | 既有 importer/normalizer → 既有 store；保存每筆 stage、provenance、storage_outcome | 只接受配置的合成來源，原始 Record 與 Evidence 分開；未知寫入結果不能當成功 |
| `count-activity`、`draft-*` | 受限 reader → 計數／空白清理／標記引用草稿 | 需要各用途許可；沒有 LLM、推論身分、發表或部署 |
| `channel-events`、`identity-records` | 明確宣告或原始 identity record → 對應 native ingest/query/history/get/export | 註冊 alias；不從人名猜身分，不推導關係 |
| `/twitch/replay`、`/twitch/archive`、`twitch-live` | 已配置且綁定的 envelope/lifecycle/archive gateway | replay/合成與 authenticated live 分開；本輪不登入或執行外部服務 |

## 輸入、原文與權限

模組 API 的公開 payload 只有 `request_id`（非空字串 ≤200）與 `text`（非空字串 ≤16384）；
scope、帳號、來源、layer、limit、time_from/time_to/current_event、ACL 來自可信 host template。
review 另限制 `review-...` 操作 ID 與 ≤2000 字元的 tool/text，完整封套及其他限制見 schema。
HTTP handler 用 strict JSON 拒絕 duplicate keys／non-finite numbers；公開 body 不複製權限或執行參數。

Entry 保留原始問句；若明示啟用 extraction，原文與抽出的 search_text、來源依據分別保存。
證據的 source_id、record_id、revision、event_time、actor、scope、ACL、SourceRef 與原文保留，
用途政策另綁精確內容雜湊和修訂。event_time 的上界不是 received_at 的歷史可用性快照。
瀏覽器投影只隱藏私有 argv/cwd/path，原始證據與同次 prompt 仍顯示；私有原始收據不經任意檔案端點公開。

## 狀態、重送與修訂

原生本機入口 exit 0＝evidence_ready/no_match，1＝其他 held，2＝執行／設定／接口錯誤，
3＝允許的 prompt 已準備且 `answer_generation_not_enabled`。exit 3 不是生成成功。
library 的 mode 與 CLI 同樣只接受 evidence/prepare；非法 mode 在任何檔案 I/O 前拒絕。
HTTP 200 仍須檢查 `execution_status`、`execution_error_code`、`pipeline_exit_code`、result.status、partial
與 source_unchanged；不能把 Entry 先前產出的 evidence_ready 蓋過稍後的來源變動錯誤。

review 同 workspace、同操作 ID、同 payload 回傳已保存的收據（replayed=true），不再呼叫 native；
同 ID 改內容或跨 Fixture/native 重用回 409。新 ID 表示新操作，不改寫前筆輸入。
reset 建立新 workspace，不覆寫或刪除舊證據；web replay 不跨 reset／重啟持久保存。
各 store 的 idempotency、修訂與撤回語意仍以其 native 收據為準。
native reader 使用目前有效修訂，review 不補造 Record 欄位、不授權回退舊版或讀取歷史。

## 已有證據與本輪限制

先前同版實測 8 個 native handler 整合測試全部通過，涵蓋 15 份操作收據；
實際 5-repo snapshot 為 `c9d9637d2ad9f2577c8f77320874b2d1d98702554fc0e0049831fdeff67a91be`。
原始資料：專案 `evidence/development/native-review-web-tests/6349288e530b4853b899724a82955173`；
runner：`native-review-web-final-1935a247247a44908b6b7f8303922e87`。
其後依賴有更新，不能把該輪視為所有最新版都通過；本輪新增契約／mode 邊界另留精確測試收據。
未改內容的 493 Python／11 PowerShell 歷史結果只沿用既有範圍，不重新宣稱本次全量同版。

本輪程序結果：mode 邊界 10 項通過、接口契約 5 項通過、web 回歸 17 項通過／6 項 HTTP 跳過。
單筆新增 native prepare 已到 prompt，但 collector 的 `tests/test_interfaces.py` 在執行中改變，
故實際回 exit 2／`source_changed_during_execution`，整合測試 class setup 失敗；不算穩定通過、不追跑。
6 份新機器示例另逐一核對既有 c9 真實收據，全部吻合，沒有重執行原生鏈路。

接收 identity 新契約後，補了 empty extensions 相容性及 5 個案例。該組完整 25 項測試因
預設 sandbox AC/Temp 下 mkdir `native` 與 TemporaryDirectory cleanup 遭 WinError 5，全部在 setup／cleanup
失敗，沒有功能斷言通過，也沒有接續執行 native identity CLI。沒有換 TEMP／路徑／工具重試，
暫存清理結果未確認。最新版接口契約 5 項及 Python/JSON 靜態解析另通過；不代替 identity 功能驗證。
詳見 `docs/evidence/interfaces-20261105/DELIVERY.json`，原始拒絕與錯誤未刪除。

共通「預設瀏覽器實際可開啟運作」仍 **BLOCKED**：既有 connect/WinError10013、
server／file URL／瀏覽器等拒絕不重試，不換埠或工具繞過。程式／mock DOM 通過不替代畫面驗收。
未測：真資料、登入、真 LLM、公開輸出、最新全 repo 穩定整合、真瀏覽器。

## 精確交接與下一步

1. **G01**：planner 送一般 `chat_history`，chat-search 原生是 last_said/question_history/history；
   question_only 同時可存在於 history，無唯一已同意映射。需總控決定 owner、選擇條件、身份／時間上下文。
   既有 recent_utterance_to_last_said_v1 僅明示 opt-in，不擴大成通用映射。
2. **Importer/store → reader**：Evidence 缺完整 Record metadata/ACL/source/revision，不能直接餵 reader。
   總控協調原始 Record 交接；本 repo channel ingest 已分別核對 importer Evidence 與原 Record，
   這次 review 只讀既有固定合成 Record snapshots，沒有把此缺口包裝為已解決。
   identity-store 已有 `record-get`（精確 scope/source/event/revision + 獨立 trusted read），
   ssr-chat `IdentityRecordGateway` 的 get/history 也已呼叫該出口，不需新增 API。
   identity 的 native 契約明定 optional `extensions={}` 必須原樣保留；本 repo 現支援這個空物件，
   並要求 Evidence／Record／原始宣告的欄位有無一致，非空物件仍拒絕，宣告雜湊不自動補寫。
   identity 工作台的空 source_refs 與 gateway 的單一 self-reference 是不同 profile；
   本次不改兩者，不在入庫後造引用，需總控明定來源宣告/profile。
   STT store 目前仍是 Result.Evidence，reader 另需完整 `{event: Record, valid: bool}`；
   source/ACL/schema_version/record_kind 與 valid 不可從 Evidence 猜補。契約位於其
   `acceptance/interface_contract.json`、`acceptance/INTERFACE_AUDIT.md`；完整 Record 與 valid 的提供 owner 待協調。
3. **最新穩定版本**：其他 owners 完成後，由總控固定 5-repo 指紋再一次整合；期間變動只能標不穩定，保留原收據。
4. **模組維護**：legacy 的 ModelAdapter/queries/TextOutputAdapter 與 native 的 PipelineConfig/命名 gateway 分開；
   固定 web registry 是受限 consumer，不另訂 reader 語意。新增工具需明確註冊、host policy、契約案例與真實收據。
