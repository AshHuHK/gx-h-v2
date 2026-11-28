# 已保存酵母配方換料復用

候選版本 `6077e26` 在新的 S288C 葡聚糖 1,3-β-葡糖苷酶隊列上計得 **1/4**。四個唯一登錄號中，P15703 的目標 EC 催化註解有實驗文獻指標 PMID 2509432；P23776、P32603、P52911 在本次查詢返回空值，均保留在分母。這個數值衡量指定 ENZYME、Swiss-Prot 快照的文獻指標覆蓋。酵素是否在特定條件下有活性，仍須讀原文的實驗材料與讀數。[四筆原始登錄與定位](candidates.json) · [逐列文獻指標](pointers.json)

海藻糖原題 `gxr_01m8d9xyaych3h1z2b` 真實保存了 `accession-pmid-coverage@1`。本次在隔離部署安裝相同摘要的插件，將已關閉的原工作區複製到獨立狀態，用正常 `gx-p0 message --successor` 與 `gx-p0 research` 開啟新的獨立 Goal。受控 Lead 從方法與配方目錄取得原版本，啟用指定方法，把新輸入綁回原摘要 `74d6d077301821523c0178e9ef5d2ae4ff03871f19ced4eec3c5bd3ee0cf7a58`。本次分類為 `SCRIPTED_CALLBACK` 加實際 CPU 計算，正式 A／A′ 的模型自主性另行評分。

|新 Run `gxr_01m8g2aj9bq8m0mpth`|可核對的效果|
|---|---|
|事件 188|當題 Lead 採納兩步配方。沿用舊配方的順序依賴，重新執行校準。|
|事件 200|校準 CPU Job `gxj_01m8g2bfxebzep7tcj` 成功。|
|事件 212|本題 CPU Job `gxj_01m8g2bgcjbnfqv5w7` 發布 `coverage.json`，原件 `gxa_01m8g2bgxw3ekq34v3`。獨立集合運算同得 1/4。|
|事件 216|首次關題只引產物，缺少使用的具名讀數，GX 拒絕。|
|事件 219、224、225、228|同一決策回合收到具名列 `b55278d283b3`，提交覆蓋與後續核文範圍，決定獲採納並結題。修正期間新增 0 個 Job。|

原酵母配方及其校準來源維持原始摘要。新題的候選與指標原件都與原題相異；三個缺指標的登錄號列為需查證。唯讀檢查核對新題的兩個 Job、決定 ID、原產物與正式報告摘要；歷史父題的結題決定、Job 與宿主回合數亦已核對。[事件、回合與 Job 回執](result.json) · [GX 正式報告](report.md) · [首次入口與提交錯誤](first-attempts.json)

從產品根目錄執行下列命令。將變數設為父題的封存 `state` 目錄，以及 `enzyme-food-20260128-sprot2026_03/lookup.sqlite`。驗收會在指定的新目錄內建立獨立插件登記與狀態。完整的 SQLite 不隨本回執再發布；小型輸入及其來源版本已隨附。

```bash
GX_CODEX_OFFLINE=1 .venv/bin/python -B acceptance/harness_final/macro/same_domain/run.py \
  --source-state "$archived_trehalose_state" --index "$enzyme_lookup" --work "$new_study_directory"
.venv/bin/python -B acceptance/harness_final/macro/same_domain/run.py \
  --verify-closed --work "$new_study_directory"
```
