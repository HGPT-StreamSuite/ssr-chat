# 本機 API 排空與停止

API 預設啟動方式及 HTTP 回應不變。選用 `serve(..., lifecycle_dir=...)`（CLI 為
`serve --lifecycle-dir <新目錄>`）時，啟用此程序自己的 POST 工作計數與本機檔案控制。
控制目錄的父目錄須已存在；每次啟動必須給未存在的新目錄，不重用舊 owner。

控制目錄只接受本機磁碟路徑，拒絕 UNC、網路磁碟、symlink 和 junction。請以作業系統
ACL 限制為預期的本機操作者可讀寫；本功能不能防禦同一個 OS 帳號的惡意程式。
`owner.json` 含隨機世代、控制 token、PID、loopback 綁定位址／埠及目錄身分，屬私有檔案，
不可提交 Git 或公開。伺服器逐次核對 owner bytes、file ID 及目錄 file ID。

使用相同 uv 專案環境執行：

```powershell
uv run python -m ssr_chat.api_lifecycle status --directory C:\private\api-generation-001
uv run python -m ssr_chat.api_lifecycle drain --directory C:\private\api-generation-001
uv run python -m ssr_chat.api_lifecycle stop --directory C:\private\api-generation-001
```

`drain`／`stop` 只原子提交 `command.json`，成功輸出 `submitted` 與 request ID，**不表示
動作已執行或停止已完成**。伺服器約每 0.1 秒核對世代／token 與命令，再更新
`status.json`；用 `last_command.request_id`、`state` 和計數確認結果。一次只提交一項命令，
等上一項已被接受再提交下一項，避免單一命令槽被覆寫。重複 request ID、不同世代、錯誤
token、額外欄位或損壞 JSON 都不授予控制；沒有透過 HTTP 暴露 drain／stop 端點。

排空後所有 POST 路由在讀取 body 或呼叫 gateway 前回 503 `api_draining`，包含 `/chat`、
`/query`、檢索、回答及 Twitch archive/replay；GET health 仍可查閱。已接納的 POST 可完成，
直到回應交付或發生例外後才結算。計數滿足 `accepted = completed + active`，`errors` 包括
HTTP 5xx、未處理例外及回應交付失敗，`rejected` 計排空後拒收。HTTP 4xx 是已完成驗證拒絕，
不增加 errors。`GET /health` 在啟用時回 `ready`／200 或 `draining`／503，包含上述計數，
不包含 owner token、世代或私有控制路徑；這只表示 admission 狀態，不代表 provider 健康。

`stop` 先排空；active 非零時保持 `draining`，不強制中止模型、子程序或請求。全部工作結算
後才關閉 HTTP server；`server_close` 也等待處理執行緒，最後狀態才是 `stopped`。啟用時
HTTP idle header/body socket timeout 為 30 秒；工具／模型本身若卡住，停止仍可能等待，
需以其既有 timeout 與觀測資料處理，不能用「暫時無連線」推論已排空。owner 身分／狀態
寫入不可用時會關閉 admission，等已接納工作完成後回報控制失敗，不採用其他程序的 owner。

此功能只作用於採用新程式並明確選用的本機 API，不修改既有常駐程序、正式設定、資料、
啟動項或其他服務，也不會強殺 PID。既有 b893 API 沒有此計數器，新增功能不能追溯證明
它已排空。正式 2B 切換仍須另行核對維護窗口、全部 writer／caller、舊服務已接請求、
停寫備份及獨立控制器；本功能不解除原有切換阻塞。

離線驗證：`uv run python -m unittest discover -s tests -p test_api_lifecycle.py -v`。
這些測試使用記憶體 handler、執行緒及暫存檔，不綁 socket、不接現有服務、不呼叫模型、
不執行人工關聯或公開訊息。它們不等同真實 HTTP／程序切換驗收。
