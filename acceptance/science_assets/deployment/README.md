# 科研資產的安裝、普通入口及回退

完整研發的起點是 `science/multi-workflow@595649e`。精簡部署在 `integration/science-plugin-reviewed-4759c26@688ab0e` 的獨立工作樹開發；該分支原有的書目覆蓋插件和 CLI 配置入口保留。現場初裝版本 `db10336` 已用線性肽結題。整合候選 `integration/science-assets@1ceb8e9` 含完整研發到 `9c69878` 的相關產品修正、30 個 pin、獨立 Python 環境及 Cordis host。兩次部署使用不同的研究狀態目錄。[精簡候選的 30 個實載插件](slim-offline-plugins.json) · [完整研發的 29 個實載插件](../inventory/installed-verify.json)

## 啟動

下列命令在所選工作樹根目錄執行。需要 Python 3.11、Node 22.17、已登入現有 ChatGPT 帳戶的官方 Codex，和既有 Clash 的 loopback 連接。部署實測分別從鎖定的本地 wheel 與 npm 快取安裝。已裝好可從第三段直接開題。[Python 安裝記錄](python-install.log) · [Cordis 安裝記錄](cordis-install.log)

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --no-index --find-links "$GX_WHEEL_DIR" -r requirements.txt
npm ci --offline --prefix gx-h/runtime/plugin-host --ignore-scripts --no-audit --no-fund
```

酵母原始索引留在可用的唯讀快照掛載，隨版提供身份、樣本、來源與獨立 SQL 檢查。將 `GX_YEAST_SNAPSHOTS` 指向該來源。肽和 Fab 的小型公開原件隨發行提供。[快照取得條件](../enzyme/sources/README.md)

```bash
python acceptance/science_assets/enzyme/mount.py --harness-root gx-h --source "$GX_YEAST_SNAPSHOTS"
bin/gx-p0 plugin verify org.gx.yeast-enzyme-archive@1.0.0
bin/gx-p0 plugin verify org.gx.archival-molecule-qc@1.0.0
```

在部署的既有官方 OAuth 配置裡使用下列命令。可用 PATH 上的 Codex，或在環境中用 `GX_P0_CODEX` 指向已配置的官方二進位。獨立研究更換 state 及 run key。選擇原 Run ID 會接回該研究。

```bash
. .venv/bin/activate
asset_dir=acceptance/science_assets
study="$PWD/var/natural-yeast-study"
bin/gx-p0 research --state "$study" --run-key archive-question \
  --task-card "$asset_dir/deployment/yeast-task.json" \
  --goal-config "$asset_dir/deployment/yeast-goal.json"
bin/gx-p0 research --state "$study" --run-id "$RUN_ID"
```

本工作區的現場設備由完整研發樹的 `deployment/start_native.py` 配置官方 OAuth、Loopback 代理及私有會話目錄，然後 `exec` 精簡樹自己的 `bin/gx-p0`。GX 產品、Python、Cordis、資料綁定、狀態及 Job 全在選定樹執行。該驗收輔助程式需要完整研發樹的宿主測試模組；已配置的精簡部署直接使用上面的普通 CLI。

## 複驗與回退

線性肽 Run `gxr_01m8fdp0m22cmw5s8d` 的兩個新 CPU Job 與五個原生回合在精簡樹完成。結題後重開同一 Run，數量仍為兩個和五個。[決定及讀數](peptide/DECISION.md) · [Job、結果送達與關閉事件 175](peptide/receipt.json) · [冷重開](peptide/warm-recovery.json)

離線的 67 項精簡版測試涵蓋目錄與資產、舊版決策、流式交接、恢復與方法重用。第一遍有兩個歷史 TSV 缺欄的錯誤，修正後通過。總表記錄這個中間候選的源碼狀態、測試分組和從完整版借入的兩份測試檔。[首次失敗](slim-offline-asset-first.log) · [離線總表](slim-offline-summary.json)

切回部署時，將啟動位置和虛擬環境切回先前工作樹，並使用它相配的舊 state。新版進行中的題目保留原 state、插件版本與執行樹，需續題時用原 Run ID。原 `688ab0e` 工作樹、舊狀態庫、既有插件與原始快照均保留。完整研發可在獨立 worktree 比對 `595649e`。這次沒有要求覆蓋遠端分支。

兩個候選共有 51 個本輪更動過的產品路徑，其中 49 個位元相同；差異為精簡部署原有的 CLI profile 註冊和一個額外已安裝的書目插件。官方 Codex 二進位 SHA 與既有發行鎖相符。最終候選另外通過 10 項壓縮原件、具名埠和普通 Goal 配方回歸。[逐檔 SHA、Python 與 Node](release-comparison.json) · [最終候選回歸](slim-field-fixes.log)

整合候選另以普通 `--task-card` 啟動麥芽糖研究 `gxr_01m8fekb5gbjkvze1w`。凍結後的分項與任務卡相符。真 Lead 在三個回合中選法、派出新 CPU Job `gxj_01m8femjkrqb8xkr8q`、實讀 `parent-records.json`，於事件 80 引用該結果、81 結題。兩個命中的 S288C accession 中，直接反應指派 0/2，較寬 EC 類別 2/2。重開新增 0 個 Job、0 個回合。[任務卡、材料、實讀呼叫與決定](yeast/task-to-action.json) · [Lead 的完整結論](yeast/DECISION.md) · [恢復記錄](yeast/warm-recovery.json)

## 兩份發行的 Goal 材料

已在完整版與精簡版各核對 12 份可攜 Goal，包含首次實驗設定、更新後的設定和無附件文獻題。每版有 21 筆材料綁定，對應 13 組不同的實際 SHA-256。全部材料、配套題面、7 份取得描述都能從各發行自己的相對路徑讀取，兩版內容逐位元相同。5 份取得描述另列材料摘要；1UBQ 與 1MBN 的既有描述由 Goal 材料 pin 綁定，描述本身亦各有實算摘要。兩版的 6BEN 原件及其刪除一項聲明的衍生控制均在 `acceptance/science_capabilities/structure_controls/materials/`，精簡版的 `examples/deposited-structure-controls/macrocycle.json` 也引用相同兩檔。精簡版已補上文獻題的原題與零附件 Goal。此項是檔案可攜性查核，酵母方法用的唯讀外部快照仍需依上述說明掛載。[兩份發行的逐 Goal 檔案摘要](portable-inputs.json)

```bash
python acceptance/science_assets/deployment/portable_inputs.py . ../science-assets-slim
```
