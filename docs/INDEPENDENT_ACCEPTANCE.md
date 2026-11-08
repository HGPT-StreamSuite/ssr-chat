# ssr-chat 獨立本機驗收

本標準的工作起點為 `codex/streamsuite-live-lifecycle`、HEAD
`541c296d0f63fe4a83eed32a2beaaeea2e69fae7`。實際受測版本必須另外記錄 HEAD、dirty
state 與 source fingerprint；不能將新增程式視為已包含在上述 commit。

## 所有 repo 共通門檻：預設瀏覽器可開啟運作

**本輪狀態：BLOCKED／未驗證。整體網頁驗收尚未通過。**

所有 repo 採相同門檻：必須在使用者的預設瀏覽器實際看到頁面，並完成適用的輸入、
實際輸出與錯誤操作，保留真實操作證據。程式存在、HTTP 成功、後端測試、假 DOM／fetch
測試都不能替代此門檻。預設瀏覽器身分與實際開啟在本輪均未完成驗證；可用瀏覽器的盤點不算開啟證據。

本輪 loopback socket.connect 已遭 WinError 10013 拒絕，因此沒有啟動 8826 或開啟
該頁面，也未改用 file URL、其他埠、瀏覽器或執行器繞過。這個共通門檻不能降低或豁免。

## 範圍與輸入

本 repo 負責文字事件驗證、觸發、前文、用途查詢、回答器呼叫、dry-run 輸出及追蹤。
網頁預設執行固定的原生頻道工具：本 repo 的 local_pipeline／EntryGateway 經 planner、dispatcher、
channel-search、collector 回傳實際合成證據，prepare 模式完成用途檢查與 prompt 後明確停止，不呼叫 LLM。
可另選舊 `ChatRuntime` Fixture 事件；該回答器是確定性的測試替身，不是真實 LLM。
兩條路徑都不連 Twitch、Ollama、正式資料或公開輸出。

輸入為明確 `synthetic=true` 的 JSON 封套與事件。事件來源只能是 `local-fixture`，
聊天室只能為 `review-room`／`review-other-room`，帳號與訊息 ID 使用 synthetic／review 命名空間。
只接受頁面列出的欄位，不接受路徑、模型、端點、權限、資料庫或執行命令設定。
每次啟動建立全新的合成暫存目錄；「新工作階段」建立新子目錄，不接續既有資料。
原生輸入為 `{tool,text}`；tool 只接受 channel-evidence、channel-prepare、channel-purpose-denied。
Python、五個 canonical checkout、reader 命令、snapshot、scope、ACL 與用途政策由固定註冊建立，
不能從問題或頁面 JSON 提供。註冊資料是總控 6adff805… 收據鏈使用的原樣合成資料，逐檔驗 SHA-256。

## 本 repo 功能測例

| ID | 輸入／操作 | 預期輸出與成功判定 |
|---|---|---|
| WEB01 | 合成 `!ask 星界遠征是什麼遊戲？` | 真正呼叫 ChatRuntime；`answered`，非空 fixture 原文，`dry_run_not_sent`、`attempted=false`；回傳本次 prompt 與 trace |
| WEB02 | 同聊天室接續「那款遊戲還有哪些特色？」 | prompt 的 snapshot 包含前一事件；另一聊天室不混入該前文 |
| WEB03 | 缺 message_id、未觸發、自身訊息 | 分別 `held/missing_message_id`、`held/not_triggered`、`held/self_event_loop_guard`；不呼叫回答器 |
| WEB04 | 新 request_id 重送相同事件 | `held/duplicate_message_id`；不再次呼叫回答器 |
| WEB05 | 同 request_id／相同內容再次提交；同 ID 不同內容 | 前者回同一 operation 且 `replayed=true`，不再次執行；後者 HTTP 409 `request_id_conflict` |
| WEB06 | 非合成、外部 source、任意路徑／設定欄位、錯型資料 | HTTP 400，明確 reason_code；在 ChatRuntime 前拒絕 |
| WEB07 | 損壞 JSON、重複 key、NaN、過大 body | HTTP 400／413；頁面顯示真實錯誤，不填入示範成功結果 |
| WEB08 | 非本機 Host、跨 Origin、缺少頁面 token | HTTP 403；不執行事件，不回傳任何工作紀錄 |
| WEB09 | 新工作階段；舊 workspace 提交 | 新階段重新建立隔離 runtime；舊 workspace HTTP 409；不採用其他目錄或既有 owner |
| WEB10 | 回答器拋錯／未知路由 | 前者保留 runtime 的 failed/model_error 與未發送；後者 HTTP 404；不暴露伺服器檔案路徑 |
| WEB11 | 取消「新工作階段」對話框、返回輸入、快速重複點擊 | 取消保留輸入與輸出；返回保留輸入並聚焦；忙碌時不能再次提交 |
| WEB12 | 桌面與窄視窗、鍵盤操作、含 HTML 的輸入 | 控制項有 label／焦點；頁面不橫向溢出；JSON／回答以純文字顯示，不能執行 HTML |
| WEB13 | 驗收標準與證據檢視 | 可查閱本表、來源版本、當前 operation 證據；歷史回歸明確標為歷史，不能套用為新網頁已通過 |
| WEB14 | G01 `chat_history` 映射缺口 | 只供閱讀、明示 blocked／未接通；不改 planner、native intent、scope、ACL、questionflag 或時間界線 |
| WEB15 | channel-evidence，/channel launch | 實際執行 planner、dispatcher、channel-search、collector，pipeline exit 0、evidence_ready、2 筆原生證據；LLM 與公開輸出均未嘗試 |
| WEB16 | channel-prepare，/channel launch | 同次原生證據與用途檢查後得到完整 prepared prompt；exit 3、held / answer_generation_not_enabled；不是回答、不是已送 messages |
| WEB17 | channel-purpose-denied，/channel launch | 實際原生檢索後由既有 prohibited 用途政策拒絕，exit 1、ai_input_not_allowed；沒有 prepared prompt 或模型呼叫 |
| WEB18 | channel-evidence，/channel absent-synthetic-term-92837 | 原生 no_match、exit 0、零證據；不回退 Fixture 或補猜 |
| WEB19 | /unknown launch；/channel launch /history anything | 由真正 EntryGateway 回指令錯誤／巢狀指令拒絕，exit 1；不啟動 reader、模型或其他資料域 |
| WEB20 | request／封套夾帶 access_scope、command、repo、python、policy 或未知 tool | handler 在原生執行前回 400，固定註冊與 ACL 保持原樣 |
| WEB21 | 同 operation ID 重送、同 ID 改內容 | 同內容讀取該次收據，沒有第二次 native reader；改內容回 409，不覆寫舊收據 |
| WEB22 | 讀取原生結果／prompt／收據 | 原問句與 request_id 可對回私有收據，保留原生證據內容／來源／修訂；頁面顯示真正 stage exits／hash，隱去 Python／repo／artifact 絕對路徑 |

所有正向結果都必須同時檢查 status、業務 assertions 與 dry-run flags；HTTP 200 不等於功能成功。
前端取消對話框不是取消正在執行的 runtime；此頁不提供誤導性的執行中取消功能。

## 證據與完成定義

程式測試需保存精確 command、環境、合成輸入、exit code、assertions 與實際輸出。
網頁 QA 需另外記錄輸入／輸出／錯誤／重複／取消／返回、桌面與窄視窗結果。
沒有瀏覽器驗證時，WEB11–WEB12 標為未測，不得由 HTTP 測試推定通過。
證據中的 source fingerprint 排除可變的驗收紀錄及暫存狀態，以免自我引用。
本次產物只有本 repo 網頁；不含總頁面、發布、部署、正式平台或模型端到端驗收。

## 已實際驗證結果（程式層，不能替代共通門檻）

- 隔離 ChatRuntime 真正執行合成事件，回 Fixture 原文、prompt、trace 與 dry-run 未發送旗標。
- 同事件重送回 duplicate_message_id，回答器只呼叫一次。
- 無 socket 的 handler 分支驗證：JSON、Host／Origin／token、路由、重設、scope 命名空間與錯誤。
- 假 DOM／fetch 的 JavaScript 程式測試驗證忙碌、重送、取消、返回與 tab 狀態；不算實際瀏覽器操作。
- 精確指令、通過數、source fingerprint 與未測結果見 docs/evidence/review-web-acceptance.txt。

## 不適用與理由

- 本次不建立跨 repo 總頁面、導覽或部署：使用者明確排除；不能算作通過。
- 本頁固定 Fixture／dry-run，不提供登入、正式模型或公開發送控制：避免本機檢視操作改動真實狀態。
  這些真實整合仍是整個機器人的未完成項，不以「不適用」豁免其後續驗收。
- 不提供正在執行的 runtime 取消：此頁為短程、同步 Fixture 操作，沒有安全取消協定。
  只提供尚未執行的新工作階段對話框取消及返回輸入，不能把中止瀏覽器等待稱為停止運算。

## 未測／目前環境阻塞

2026-11-05 本輪首次真實 loopback HTTP 測試在 `socket.connect` 遭 Windows
`PermissionError / WinError 10013` 拒絕。已停止該動作，未換埠、shell、執行器或改用瀏覽器重試。
8826 服務未啟動，沒有已驗證的可用網址。WEB11–WEB12 與真實 HTTP transport 仍未驗證。
後續無 socket 的 handler 測試只證明程式分支，不等於網頁或 HTTP 已跑通。
真實 HTTP 測試保留為需明確 opt-in 的測試；須先取得解除該拒絕的相關新授權，不能自行啟用重試。

## 啟動與停止

在 repo 根目錄，使用既有 uv 環境（不安裝或同步）：

```powershell
$env:UV_PROJECT_ENVIRONMENT = 'C:\Projects\StreamSuite\runtime\envs\development-live-lifecycle'
uv --cache-dir C:\Projects\StreamSuite\runtime\uv-cache --offline run --no-sync python -B -X utf8 -m ssr_chat.review_web --port 8826
```

先確認 8826 未被使用；占用時停止，不終止其他程序或自動換埠。
服務只綁定 `127.0.0.1`。啟動後開啟印出的 URL；在同一終端按 Ctrl+C 停止。
停止時只清理這次服務自己新建的合成暫存目錄，不清理任何既有資料。

## 尚未完成

- G01：一般 `chat_history` 到 `history`／`question_history`／`last_said` 的 caller 映射與 owner 待定。
- 真實 Twitch 接收／發送、Ollama 推論、正式資料與跨 repo 整合，皆不在此網頁可操作範圍。
- 舊 493 Python／11 PowerShell 證據限先前來源版本；本次新增內容必須另驗。
