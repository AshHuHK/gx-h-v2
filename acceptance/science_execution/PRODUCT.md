# 決策短表單工程驗收

三個獨立 Run 使用真實 CPU Job。P4 透過官方宿主與本機腳本對端，另外兩案使用本機原生投遞回執。此次工程驗收的模型推理呼叫為 0。事件編號屬於表中各自的 Run，完整識別、讀數、提交與採納摘要收錄於可攜式回執。[工程回執](evidence/product.json)

| 案例 | 實際發生的事 |
| --- | --- |
| 處置錯誤 | 同一 Goal 受控提交兩次。使用舊規則時，正確 ID 加空操作、錯誤 ID 加顯式 MAINTAIN 都得到同一句錯誤。新規則分別指出需修正的 `ops` 或 `comparison_id`，事件 37、38 留下拒絕紀錄，兩次都保留先前的圖。原 OAuth 現場與離線對照各有其證據來源。[歷史核對](HISTORY.md) |
| P4 短表單 | 事件 70 已送達具名 coverage 讀數 149/164。事件 79 第一次讀後決策就接受，提交僅含 `rationale`、`metric_refs`、`ops`。GX 綁定原比較並採納 MAINTAIN。對應派發節點的參照量測 Job 在事件 90 成功。 |
| 第一筆便宜量測 | 事件 35 採納短表單。事件 47 記錄 CPU Job 成功，下一份已送達簡報以該事件喚醒，帶有同一筆結果引用。重開資料庫後重送原請求，採納回執一致，Job 總數仍為 1。 |
| 委派報告讀數 | 實際量測產物進入模組報告。Lead 取得的 coverage 列為 49/164，事件 90 留存送達回執，事件 97 的結題決策使用該端點及報告引用。測試另驗證只填顯示值、名稱或未送達來源會被拒絕。 |

下列 16 項測試已通過。在倉庫根目錄執行。第一個模組重跑兩種規則的實際拒絕，並核對封存短請求及正規化摘要、送達列、派發、Job 終態及重送證據。其餘模組以新的 Goal 和 SQLite 狀態執行相應回歸。[重播與探針](test_product_receipts.py)

```bash
PYTHONPATH=.:gx-h:gx-h/tests GX_CODEX_OFFLINE=1 .venv/bin/python -B -m unittest -v \
  acceptance.science_execution.test_product_receipts \
  acceptance.v624.test_selected_rows acceptance.v624.test_cheap_job \
  acceptance.v624.test_comparison_inference
```

要另起本機正式宿主預演，在下列命令傳入新輸出目錄與現有 Clash 二進位檔；每次新 Run 的識別及事件時間隨實際執行產生。[兩案預演入口](rehearse.sh)

```bash
bash acceptance/science_execution/rehearse.sh "$NEW_OUTPUT_DIR" "$CLASH_BINARY"
```
