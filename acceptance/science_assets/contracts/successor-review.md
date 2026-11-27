# 科研產物移交與任務卡覆驗

父研究 `gxr_01m8fefhhv8dya58eh` 的 CPU Job `gxj_01m8fefk7jbfcfx2cc` 產出 `signed-summary.json`。後繼研究 `gxr_01m8fefnh7sm1kcycs` 同時收到該具名產物與 PI 獨立附上的同名、逐 byte 相同檔案。兩個本地 Artifact 各自保留來源。後繼在新座標上實際執行 CPU Job `gxj_01m8fefphqg7e4eqhk` 並結題。這組驗證由腳本提供 Lead 決策，透過正常 Goal、實際 Job 與發布入口執行。[運行與移交回執](review-ordinary-goals.json)

| 條件 | 實際驗證 |
| --- | --- |
| 不同來源交付同 bytes 和同檔名 | 新 Run 保留不同的 Artifact。具名分析保留原 Run、Job、具名輸出口與摘要。PI 裸檔保留未分類。兩者均無新原始觀測。舊 Run 的衝突資料按 `ambiguous_transfer` 讀取。 |
| 截止點與可見性 | 模組實際 CPU 輸出在尚未送達的 Lead 決策快照被拒。報告送達後，結題快照能選取具名輸出。拿報告的 Artifact 取代 Job 輸出口、跨 Run 決策或不匹配摘要均被拒。 |
| 冷重送與時間 | PI 入件後可移除兩個原始上傳檔，兩位併發請求者與重啟命令仍取回同一子 Run。交接事件以前的證據視圖看不到交接來源。相同 bytes 保有既有首次發表時間，不能重登記為新採集。 |
| 已發布的大檔 | 真實的具名 CPU 輸出在降低複製門檻後經正常後繼 Goal 成功。另以 8,912,896 bytes 的資料驗證實際超過 8 MiB 時的串流、交易回滾清理、重新登記及雙方摘要。大量複製和驗雜湊在 Run 寫入交易外完成。 |
| 任務卡 | 結構化問題、逐項驗收、停止條件與選填 effort 凍結在 Goal 內。多行項目在提示中保有階層，同一個 Run key 會拒絕改變條目分組。舊版已保存的文字仍支援重開，卡片刪除後仍可按 Run ID 繼續。 |

原版兩個失敗可見 [紅燈](review-first.log)。最終 24 項涵蓋移交、卡片、大檔、輸出來源完整性、不可見子報告及底層檔案發布。[組合回歸](review-final.log)

另有一個歷史原生 Peer 測試自備記憶體內傳輸端。以全域離線旗標執行會抑制其預期的 turn，在 PAUSED 與 COMPLETED 兩個子案例均無 turn。採用它自己的 Peer 模式再跑，兩種狀態均通過，無外部模型呼叫。[原環境失敗](review-offline-flag.log) [Peer 重驗](review-legacy-peer.log)

從產品目錄重跑：

```sh
export PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a
GX_CODEX_OFFLINE=1 .venv/bin/python -B -m unittest -v \
  acceptance.unified_multiturn.test_successor acceptance.science_assets.cards.test_task_card \
  acceptance.science_assets.contracts.test_transfer_size tests.test_v62_outputs \
  tests.test_delivery_manifest acceptance.v62a.test_round_visibility
.venv/bin/python -B -m unittest -v tests.test_p0_cold_input
```
