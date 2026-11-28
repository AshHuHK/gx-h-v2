# 可移植的多階段 Recipe

`gx_save_recipe` 在已完成的研究裡讀取 Job 及其具名輸出。選擇多個 `decision_points`、`evaluation`、`children`，或選入已執行的 `evidence_gate`，會建立 `gx.research-recipe/4`。各階段使用同一條原生 `gx_instantiate_recipe` 與 `gx_decide_revision` 路徑。

- `decision_points` 指定要讀的步驟、輸出埠及重新交給科學 Lead 的問題。每個新 Goal 的決策引用當題的新產物。
- `evaluation` 指定評估步驟及其專用留出輸入槽。GX 驗證開發資料與留出的內容摘要各異，並派獨立重算。若該留出與保存配方時用過的留出完全相同，預覽與決策簡報會標出 `same_as_source_evaluation`。內容摘要不同只表示檔案位元有別；樣本的實際獨立性由科研判斷。
- `evidence_gate` 的資料角色、具名輸出、JSON Pointer、比較值及型別、是否回交審閱，都由所選 Job 的原始輸出及本題節點映射。原件重複綁多個角色時，可指定 `source_role`。科學上要改判據，可以提交新的圖修訂或保存新版本。沿用同一配方身分的操作會核對原判據。
- 子配方用 `recipe` 和 `digest` 指定不可變版本，`steps` 和 `inputs` 將已執行的子步驟對應至親本。GX 檢查實際方法、provider、連線、具名埠和條件；遇循環、摘要錯誤、交換步驟或省略子配方要求的模組 Join／校準，都會指回有問題的選擇。執行時以同一張圖派工。
- /1、/2、/3 沿用其原始摘要。單輸出決策、模型預測、校準、換 provider、受控重算和具名資料邊由既有測試覆蓋。

## 驗證與重跑

分類為 `SCRIPTED_CALLBACK` 與 `UNIT/FAULT_INJECTION`。實際入口為 `bin/gx-p0 research`、原生工具回調、SQLite、Job runner 及插件安裝／驗證。計算使用 stdlib 的水文 CPU 插件；回調中的科學取捨由測例控制。

先在兩站原始提交各跑相同來源圖；真實 Job 完成後，兩站均於保存時拒絕 `decision_points/evaluation`。日誌為 `portable-full-before.log`、`portable-slim-before.log`。候選的普通入口重跑見 `portable-candidate.log` 和 `portable-stages-receipt.json`，含來源 Run、目標 Run、原件、Job、讀數與生效決策。

| 目標題 | CPU 派工及決策 |
| --- | --- |
| 讀數 60 分鐘，命中採納條件 | 新篩選 Job 完成，Lead 引用讀數並批准後，測例對控制器執行 SIGKILL。新程序重新開庫並重送同一回調，取回原決策。獨立評估 Job 完成後，另一份決策引用其具名產物。共 2 個 Job。 |
| 讀數 45 分鐘，未達採納條件 | 新篩選 Job 完成，Lead 引用負向讀數；評估節點 BLOCKED，零評估 Job。共 1 個 Job。此題故意再次提供來源題已使用的留出，回執顯示該材料的使用歷史。 |

另有一項舊私有產物測例在兩站均於凍結簡報過期時被拒。修正測例時序，讓初始 Lead 先正常提交等待，再讓子研究員建立節點；原來要求私有數值必須等報告交回才可使用的斷言通過。首次結果與重跑分別見 `private-scalar-*-before.log` 及 `portable-compat.log`。

從產品根目錄可重跑：

```bash
PYTHONPATH=".:gx-h:gx-h/tests:acceptance/v62a" GX_CODEX_OFFLINE=1 python -B -m unittest -v \
  acceptance.harness_final.macro.test_portable_stages \
  acceptance.science_plugin_blueprint.test_recipe_binding acceptance.v621.test_evidence_gate \
  tests.test_v6_named_ports acceptance.harness_refactor.macro.test_macro_recipe
```

設定 `GX_PORTABLE_STAGES_PROOF` 可將該次 Run／Job 及決策定位寫到指定 JSON。真模型對科學取捨的行為、外部作業接受後的故障試驗及 B 的 GPU 現場證據另列。
