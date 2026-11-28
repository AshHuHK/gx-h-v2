# 以新材料重用研究委派與證據匯合

在一般 `bin/gx-p0 research` 的 Lead 回合，`gx_watch_join` 可登記本題所需的交付。指定 `key`、`question`、`mode` 和 `ports`。每個端口有一個 `slot`，其 `alternatives` 各指定一個本 Run 的 `node_id` 或 `module_instance_id`，以及 `output_name`。具名檔案要使用該 Job 實際發表的名稱，研究員報告使用 `report`。

- `all` 要求所有端口，`any` 要求一個，`k-of-n` 額外指定正整數 `k`。標為 `required: true` 的端口在三種模式下都必須到達。多個端口指向同一份交付時只計一次。
- 已排定重試和執行結果尚未知的作業繼續等待。可選的 `timeout_seconds` 設定由 Lead 約定的期限。缺少不可替代產物、期限已至或已取得足夠產物時，GX 持久化一次結果，將具名讀數、缺口與仍在途的工作送入 Lead 簡報。Lead 可以維持、降級、更換取證或收尾。
- 重送相同規格取得原登記；改動所需證據要提交相同 `key` 和下一個 `revision`。可對先前的計算重新詢問，已完成的 Job 保有原識別碼。
- 保存時使用 `gx_save_recipe`。其 `nodes` 對應表列出已完成的計算，`inputs` 為外部材料命名。加入 `macro.modules` 表列已交付報告的委派，每項含 `slot`、`module_instance_id`、可用於下一題的 `question` 和 `expected_delivery`。設定 `macro.join_key`，有需要時指定 `join_revision` 及可重用的匯合問題。純委派可以使用空 `nodes`，純計算匯合可以使用空 `modules`。該 Lead 必須已收到匯合結果和所選報告。
- 新 Run 可用目錄中的 `recipe` 名稱及摘要呼叫 `gx_instantiate_recipe`。綁定新 Run 自己的材料、`prefix` 與 `branch`。回應含可交給 `gx_decide_revision` 的 `ops`、可由 `gx_open_module` 開工的 `module_calls` 及可送到 `gx_watch_join` 的 `join_call`。若某委派依賴尚未產出的具名檔案，`pending_module_inputs` 指出其等待端口。一個委派也可依賴另一個委派的已發表報告。若匯合規格需要尚未提交的節點或尚待上游報告的委派，`join_pending_nodes` 和 `join_pending_modules` 分別列出其名稱。提交後用相同綁定再次展開，GX 回傳已採納的步驟與剩餘工作，保留每個委派的原操作鍵。

尾段的科學決策以開放問題與待查端口保存。在新 Run 中，Lead 可以依該題實得的材料選別的分支。完整保存來源另存於 `v6_saved_recipes.sources_json`。

## 復驗

從發布根目錄執行

```sh
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B -m unittest -v \
  acceptance.harness_refactor.macro.test_macro_recipe acceptance.harness_refactor.macro.test_joins
```

`test_macro_recipe` 從兩次真實 CLI 入場，用受控 native 回呼讓 Gauge A 和 B 各委派研究員讀取各自原件，並將該題報告交給下一位研究員核查。B 的 Lead 看見新報告後採納流量比較分支，第三個缺乏計算方法的 Goal 得到明確的目錄狀態與拒絕。另有 CPU 測試在展開具名多步圖後中斷，重新開啟資料庫，用 B 原有的生產者 ID 完成消費步驟。兩個新 Job 各執行一次。`test_joins` 涵蓋獨立連線爭用、PARTIAL、重試、未知外部作業、逾時、來源損壞及隔離研究員。這些是確定性測試，正式模型現場結果由 field 目錄單獨記錄。
