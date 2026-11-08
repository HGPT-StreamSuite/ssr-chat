# 網頁到原生本機入口

預設頁面使用 `/api/native/run`，由真實 handler 呼叫
`ReviewWorkspace.run_native → NativeReview.run → local_pipeline.execute`。
後者在本程序內執行 EntryGateway，再透過原生 CLI 呼叫 planner、dispatcher、channel-search、collector。
這不是歷史結果重播，也不以 Fixture 回答代替原生結果。

輸入只接受外層 `synthetic/workspace_id/request_id/request`，內層只有 `tool/text`。
三個固定 tool 是 `channel-evidence`、`channel-prepare`、`channel-purpose-denied`。
用途拒絕工具使用總控既有的 prohibited 合成政策，沒有從 HTTP 接收或新推導授權。

`fixtures/review_native/provenance.json` 連到總控快照
`6adff80595283f0fb33ca5f50a6b1a18de6dc4ff47fe750441362c595ec939eb`，
並固定 host template、reader 設定、原生合成 snapshots 及 allowed/prohibited 用途政策的原始 bytes。
每次新工作階段在專用新目錄建立設定與收據；執行命令與 Python/cwd/checkout 綁定仍由總控原入口檢查。
保留 native_capture、prepare_only 與其他 repo 的實作。後續接口稽核只在 local_pipeline 的公開
library 邊界補上 mode 檢查，與 CLI 同樣只接受 evidence/prepare，非法值在讀檔前拒絕。

本機網頁接口版本為 `ssr-chat.review-native.v1`；`/api/meta` 的 `default_flow=native` 與
`native.interface_version` 明確標示預設路徑，原生執行回應也帶相同 `interface_version`。
這是新增的本機封套標記，不改 native `schema_version=1.0.1`，輸入沒有新增必填欄位。
機器可讀輸入 schema、正負向示例與全部入口索引見 [SYSTEM_INTERFACES.md](SYSTEM_INTERFACES.md)。

頁面結果包括同次原生 entry result、records、prepared prompt、stage 與 reader 的退出碼／hash，
以及五 repo 的執行快照 hash。公開投影去掉私有 command/cwd/路徑，完整原始收據留在這次合成工作目錄。
prepared prompt 未送入模型；`answer_generation_not_enabled` 的 exit 3 是明確停止邊界，不是生成成功。
舊 Fixture 事件保留在另一個明示選項，不會作為原生錯誤的 fallback。

實際整合測試只建構記憶體 handler，使用真正原生 CLI，不建立 socket：

```powershell
$env:UV_PROJECT_ENVIRONMENT='C:\Projects\StreamSuite\runtime\envs\development-live-lifecycle'
uv --cache-dir C:\Projects\StreamSuite\runtime\uv-cache --offline run --no-sync python -B -X utf8 -m unittest discover -s tests -p test_review_native.py -v
```

測試收據在專案 evidence/development/native-review-web-tests 下新建的獨立目錄；不清理舊紀錄。
若第一個原生 prepare 檢查失敗，停止該測試類別，不自動換執行器或路徑繞過。
既有 connect/WinError10013、CIM/Registry、file URL、server 與預設瀏覽器拒絕維持原狀，沒有重試。
共通預設瀏覽器門檻仍為 BLOCKED；此接線不代表網頁或完整機器人驗收通過。
