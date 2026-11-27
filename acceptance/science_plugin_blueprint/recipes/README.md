# 換材料後重用研究配方

配方保存方法、具名資料邊與交給下一位 Lead 的問題。運行者可在新的 Goal 使用其他原件。Lead 的讀後行動連同該 Goal 的證據獨立保存。[工具說明](../../science_capabilities/RECIPES.md)

## 控制驗收

| 控制 | 實際觀察 |
|---|---|
| 來源覆蓋 | 海藻糖資料為 2/3，蔗糖資料為 0/1。第二個 Goal 產生了新的覆蓋 Job，原校準回執可引用。受控原生回調分別提交優先閱讀已連結論文及尋找外部來源的決定。[兩題原始回執](coverage.json) |
| 改變校準 | 更換方法來源引文後，後題重新完成校準及覆蓋兩個 Job。獨立測試也覆蓋方法資源位元變動、適用範圍變動及主動要求獨立重複。[重算回執](recalibration.json) |
| 歸檔儀器預報 | 原序列外推 7，更換觀測後外推 −1。一般重放加一次明確的獨立重複得到兩個 Job。重新開啟資料庫、重送同一決策後仍為兩個。每份預報保留 model_prediction 及 readings 輸入角色。[節點及 Job](sensor-prediction.json) |
| 公開座標控制 | 從 wwPDB 1QYS 固定選取五筆 Cα 原始記錄，保留原始 92 對映射。對父題已發布的預測做離線測試，有 5/92 對可比。第二個普通 Goal 的驗收另外追蹤其本身的 Job、讀數與真模型決策。[預註冊](sparse-reference/preregistration.json) · [離線材料檢查](sparse-control-check.json) |

上述兩項紀錄覆蓋測試及儀器預報使用真實 CPU 子進程及受控原生傳輸。已保存首次工具拒絕與實際 B0 授權附檔造成的保存失敗。[首次失敗](first-failure.log) · [回歸結果](regressions.log)


## 正式公開後題結果

| 研究 | 具名讀數 | 真 Lead 生效的研究範圍 |
|---|---|---|
| 父題 `gxr_01m8f5r59yyx9gmekh` | 92/92，1.008950 Å | 採納對事前映射整段的校準，關閉研究。 |
| 後題 `gxr_01m8f6s3pam4nc58dg` | 5/92，0.787816 Å | 結論僅限五個已給位置，整體未決。記錄取得完整坐標的需求，停止重算同一摘錄，事件 378 關閉研究。 |

後題 Lead 透過原生入口找到、實例化配方並引用當次具名測量。H800 推論 `gxj_01m8f75vf3h1rk3tc6` 在 GX 冷恢復前後保留同一 runner 和 operation key，CPU 比較 `gxj_01m8f7ebh2ab8sy881` 將三筆讀數於事件 366 送達。獨立使用原始 PDB 及四元數疊合的計算差 3.51×10⁻¹⁴ Å，事前容差為 10⁻⁶ Å。後題經過 6 個真 Lead 回合，另有子研究員。兩題都執行 `close_run`，後題記錄的新增資料需求留待後續研究。對五筆覆蓋的早期預測在正式報告中仍為 Undetermined，結案引用的是實際測量。[實際工具、回合及決策](live.json) · [冷恢復](inflight-resume.json) · [獨立計算](live-independent.json) · [GX 原始報告](successor-report.md) · [正式決定](successor-decision.md)

後題首次查詢曾因原部署目錄仍有舊版輸出讀取器失敗。更新該部署並恢復同一 Run 後成功找到配方。真 Lead 在提交新分支時又有七次格式拒絕，最終自行修正；產品已在新配方展開回執列出所需分支欄位與 Job 資源合計。這項欄位提示已通過目標程式測試，本次保留首案模型結果。[目錄錯誤](first-live-catalog-failure.json) · [分支錯誤](first-live-branch-errors.json)

## 以官方 OAuth 執行公開後題

在 B0 的同一 workspace 等候原 Run 結案並發布工作流後，執行以下命令。`--deployment` 指向已安裝 B0 方法的正式部署，`--private` 指向這次研究的固定私有目錄。命令以有固定 ID 的普通 PI 後繼消息開題，再從保存的 Run 恢復。再次執行時沿用同一份消息與研究。使用既有 OAuth 與本地 Clash。

```bash
.venv/bin/python acceptance/science_plugin_blueprint/recipes/start_successor.py \
  --deployment "$DEPLOYMENT" --state "$B0_STATE" --parent "$B0_RUN" --private "$B_PRIVATE"
.venv/bin/python acceptance/science_plugin_blueprint/recipes/trace.py "$B0_STATE" "$B0_RUN"
```

科學控制使用同一已發表 specimen 與 seed。後題的更換材料為一份從同一沉積擷取的座標子集。其來源、處理以及缺乏獨立實驗重複都記在隨題材料中。[來源說明](sparse-reference/provenance.json)

本次父題已在真 Lead 作出結案決定後關閉，Lead 沒有自行保存配方。平台組隨即以 GX 既有的 Python 發布函式讀取父題完成的預測與評分 Job，留下署名與來源。父題歷史和決定保留，後題仍由其新的 Lead 選擇是否實例化該版本。若復現時遇到相同情況，可在啟動後題之前執行以下命令。[本次發布回執](source-publication.json)

```bash
.venv/bin/python acceptance/science_plugin_blueprint/recipes/curate_source.py \
  --state "$B0_STATE" --parent "$B0_RUN" --deployment "$DEPLOYMENT"
```

GX 的 state 記錄原部署目錄。後繼消息和恢復均使用該目錄的 `gx-p0`，避免把活躍狀態交給另一份部署。
