# 輕量探針與技術欄位驗收

普通 `bin/gx-p0 research` 以公開 CC0 數列工具建立 Goal。驗收腳本模擬 Codex 原生工具呼叫，實際由 GX 派發 CPU Job 並從 Artifact Store 讀回結果。數值屬工程對照。對真 Codex 的判斷請另看現場 Gate。

| 查核 | 回執 |
| --- | --- |
| `50d31f5` 的三項原始失敗 | `baseline-red.log` 保存第一次紅燈，`archive-before.log` 用 `git archive 50d31f5` 取出的核心獨立復核。兩步簡寫要求先寫完整分支，未帶版本的唯一方法遭拒，含兩個歧義的草稿只回傳單項格式錯誤。 |
| 修補後首次完成兩個 Job | `cpu-run.json`，保留首次執行。 |
| 讀回具名檔與防重送 | `final-cpu-run.json`。原始值為 4、6、8，第一個 Job 輸出平均值 6。第二個 Job 使用相同產物 ID 和雜湊，讀回均值 6。提交同一原生請求後，兩個 Job 的身分與數量保持一致。 |
| 真 Lead 的欄位誤用 | `field-labels.json`。唯讀重放現場 Run 採用的四種局部名稱拼法，逐筆對照當時首次成功提交的標籤。原始拒收次數保留。 |
| 結算喚醒 | `field-method-wake.json` 記錄追試原始回調、拒收與重試。新提交的精確方法可指向同草稿唯一受權節點。兩個候選只列出當前草稿的局部名稱；舊 Run 以及盲隔離同儕沿用原界線。定向紅綠日誌見 `method-wake-before.log` 與 `method-wake-after.log`。 |
| 跨欄位歧義 | `mixed-selection-before.log` 保存原發布只回報預算單位的拒收。候選的同一回覆列出預算單位、兩份材料與兩個已送達的比較對象，保留最初錯誤欄位。遇到未授權的方法即停止展開。 |
| 歷史協定與前瞻證據 | `archive-before.log` 證明乾淨基線原摘要一致。`legacy-wire-pre-repair.log` 是開發中候選的兩項舊 Schema 摘要偏差。發現回歸後修正新增預算單位在舊版工具宣告中的投影，`final-regressions.log` 43 項通過，涵蓋歷史摘要、重啟、已知／獨立材料及具名配方。 |

若方法、材料角色或父節點檔名只有一個契約相符的選擇，GX 可補齊。兩份候選材料、兩個可用埠，或唯一材料的角色與方法不符時，GX 向 Codex 回傳具體欄位和候選。資源帳本用 JobService 的需求量累計；單位從方法的固定指標定義讀取。Lead 要使用受委派模組的讀數，須取得已送達的產物引用。

從產品目錄可重新執行完整的定向查核：

```sh
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B -m unittest -v \
  acceptance.harness_refactor.probes.test_probes acceptance.harness_refactor.probes.test_method_wake \
  acceptance.v624.test_cheap_job acceptance.v624.test_native_decision_wire \
  acceptance.v624.test_comparison_inference acceptance.v624.test_acquisition_equivalence \
  acceptance.science_assets.diagnostics.test_decision_paths tests.test_v6_named_ports \
  tests.test_v6_prospective_jobs acceptance.v62a.test_lead_graph_isolation
```

如需保存新 CPU 回執，將 `GX_PROBE_RECEIPT` 指向新的 JSON 檔名。
