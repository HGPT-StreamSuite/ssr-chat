# 合成聊天事件持久化

`python -m ssr_chat ingest-chat --config <trusted-pipeline.json> --state-dir <dedicated-root>/<case> --input <batch.json>`

這是獨立、本機、合成資料專用的 CLI：主端核對綁定後，在各自 cwd 呼叫真實 task-20 正規化 CLI，再將相同 Record 交給真實 task-7 儲存 CLI。它不連 Twitch、不呼叫 LLM、不公開發訊，也沒有接到已運行的 8775 或正式聊天資料。task-20 接收的是已格式化 Record，不代表已實作 Twitch 網路接收器。

輸入為 `{request_id, synthetic:true, events:[{record,source,raw_reference,synthetic:true}]}`。Record 只允許共同 1.0.1 的必要欄位；原文、空白、Unicode、事件時間、接收時間與來源 reference 保留。source、actor、六維 scope 必須與 host 的合成綁定一致，Record ACL 必須符合 host 的來源可見範圍。顯示名稱、IP、徽章、額外 metadata 或 extensions 不在此切片的輸入範圍，拒絕後不寫入。只核對合成綁定，不宣稱驗證真實平台身分。

host 設定必須明確提供 normalizer 與 store 的 Python／程式目錄、獨立 context／policy、state_root 與 binding。兩端保留各自的 caller-write／source-write 檢查；local_cache 用途還須由可信來源 policy 明確允許。unknown、denied 或缺少用途允許時停止，不能用 scope allow 代替用途允許。此 CLI 不使用 search_index、ai_input、ai_train，也不因完成寫入而授予這些用途。

呼叫者的 read_scopes 必須為空，store operations 只允許 ingest；兩端的 Evidence 都必須為空。原文只保存在明確擁有的合成 SQLite 中。主端輸出與 receipts 只留狀態、階段、退出碼、Record／綁定／local_cache 授權指紋，不記錄原文、帳號或整包 native 回傳。兩個候選目前都拒絕非空 extensions，因此用途收據另存 sidecar，不擅自修改共同 Record。將來讀取或送入模型必須另行驗證可見範圍及用途，缺少收據不能推定授權。

state-dir 必須位於明確設定的專用 root 下；非空且未標記的目錄會拒絕。owner marker 綁定有效設定與候選程式指紋，已建立的 store 另外核對檔案身分。資料庫遺失、被替換或設定改變時停止，不自動補空資料庫。每批次使用本機檔案鎖；同時使用中的 state 會停止第二個呼叫，不做隱藏重試。

task-7 用 source／event／scope／revision 與穩定 payload 做持久去重；重送可回 idempotent。received_at 不改寫第一筆接收時間，相同 revision 的不同 payload 不覆寫。每筆事件獨立提交，批次會明確標示 partial；已成功的項目不因其他項目遭拒而假裝回滾。store 回覆遺失或逾時時標示 uncertain，不自動重試；重新提供同一事件後，由 task-7 核對去重。若程序剛好在首次 commit 與 owner attestation 之間中斷，現有但未 attested 的 store 會保持停止，須人工核對，不能自動信任或重置。

task-20 需要本機檔案輸入，因此主端在專用 state/work 建立短暫輸入與設定快照，正常結束後移除；程序被硬性中止時可能留下 owned 暫存，需先核對再清理。此流程僅授權合成資料，不用它匯入真實私人聊天。

測試以真實候選與獨立 SQLite 驗證正常儲存、重送、衝突、跨程序重啟、部分拒絕、讀寫分離及最小化。單元測試中的 native transport stand-in 與 acknowledgement-loss 注入須分別標示，不能當作真實 Twitch 或實際網路故障驗收。
