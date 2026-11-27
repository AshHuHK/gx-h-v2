# 已發表肽與 Fab 的檔案質控

這組入口用於回答「同一測量可以比較哪些已沉積位置」。研究者提供原始沉積，Fab 另附與該沉積對應的變域編號；Goal 下的 Lead 決定取哪些讀數。所有數值是既有座標或記錄的確定性分析。

| 問題 | 資產和方法 | 可作的判斷 |
|---|---|---|
| 兩個短肽的完整度可否用同一種分母？ | 1UAO、1LE0 的整份實驗 mmCIF；`archival-peptide-coverage@1` | 分列聚合物位置、胺基酸位置、非肽末端基團、具觀測的位點及相容構象的完整主鏈。修飾註解若缺就保留未知。[肽原件、適用範圍與獨立重算](peptide/README.md) |
| 文件能否證明有顯式環拓撲？ | 同一次 6BEN NMR 沉積的原件與移除一項閉環聲明的控制，沿用 `deposited-macrocycle-qc@1` | 從 `struct_conn` 核對原子、位置、同一副本及共價性，另外列出 20 個構象的端點幾何與覆蓋。[原件、舊控制與獨立 PDB 讀數](../../science_capabilities/structure_controls/README.md) |
| 兩條已有 Fab 重、輕鏈的哪些 CDR 可以比較？ | 4FAB、1MCP 各有自己的實驗 mmCIF、配對與 IMGT 編號。普通 `archival-fab-coverage@1` 節點使用順序為 `numbering`、`structure`。 | 對六個 CDR 分別報告已編號位點與具有正佔據率 Cα 座標的位點；驗兩條不同鏈、裝配及映射原件。方法可讀 gzip 或原始 CIF。[正反例、編號版本與獨立傳統 PDB 重算](fab/README.md) |

線性肽和 Fab 是同一個可安裝的 CPU 插件，執行環境使用發行版的 Biopython。現成公開 Fab 的配對及 IMGT 編號附件可在本版直接使用，重建編號才用到附錄中固定的 ANARCI/HMM 環境。四份實驗的線性肽／Fab 原件均隨包提供。上述資料不估計結合、療效或新序列構象。短肽報告把預測置信度記為未知，環肽只驗證實驗文件記載與幾何。

## 從普通研究與檢查入口重現

在產品根目錄操作。挑一個新的私有目錄放研究狀態。實際部署使用官方 OAuth 及指定 Clash。

```bash
bin/gx-p0 plugin verify org.gx.archival-molecule-qc@1.0.0
case=linear-peptides
cards=acceptance/science_assets/modalities/studies
study=../gx-science/science-assets-live/example-peptide
GX_P0_CODEX="$PWD/vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex" \
  .venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$study" research \
  --state "$study/state" --run-key archived-observations \
  --goal-file "$cards/$case/question.md" --goal-config "$cards/$case/goal.json"
```

`macrocycle` 和 `fab-first` 亦可從相同入口開始。在 `fab-first` 原 Run 正式關閉後，以工作區中既有方法及配方開第二課題。後題使用另一沉積及另一份 IMGT 原件。相同請求 ID 讓重送回到原後繼。原 Run 恢復可直接使用 `research --state "$study/state" --run-id RUN_ID`。

```bash
.venv/bin/python -B acceptance/science_assets/modalities/studies/run_fab_successor.py "$study" PARENT_RUN_ID
.venv/bin/python -B acceptance/science_assets/modalities/collect.py "$study/state" RUN_ID
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B -m unittest \
  acceptance.science_assets.modalities.test_installed \
  acceptance.science_assets.modalities.peptide.test_peptide \
  acceptance.science_assets.modalities.fab.test_fab
```

基線原本能查到環肽方法；兩個新方法都查無精確參考卡。[首次普通 Goal 目錄回執](first-discovery.json) 初版把 Job 腳本放在可作進程內導入的 Python 目錄，正式插件載入失敗。已移到既有 Job 腳本目錄，以同一套方法內容重新核驗。[首次載入失敗](first-plugin-verify.json) · [修後實際插件核驗](plugin-verify.json) · [十一項定向回歸](tests.log)

完整研發版的 Fab 首案與線性肽首案在此修復以前已開題，首次選法和錯誤保留。操作員從普通能力修訂入口把原先要求的歸檔能力加回原 Run。之後的 Job 與結論由原研究繼續作出。兩個範圍修復的作者另有記錄。[Fab 原 Run 修復](studies/fab-first/scope-recovery.json) · [線性肽原 Run 修復](studies/linear-peptides/scope-recovery.json) · [驗收定義與對照](studies/SCOPE.md)

## 實際普通 Goal

6BEN 首案 `gxr_01m8fdbhka9m2tahwb` 已結題。Lead 實際比較一份原件與一份衍生對照，分派兩個詳查閉環、兩個主鏈覆蓋的 CPU Job。四個 Job 均交付成功。兩份資料的模型 1 端點距離均為 1.335 Å，兩份各有 20/20 個模型可定位端點；原始聲明為 1，對照為 0。事件 163 的 Lead 決策因此只把原件用於顯式閉環計數，保留同一次沉積的觀察關係，164 結題。4 個官方回合；關閉後循原入口再入，新增 0 次模型回合、0 個 Job。[6BEN 正式決定](studies/macrocycle/DECISION.md) · [逐 Job／測量與原件](studies/macrocycle/receipt.json) · [恢復回執](studies/macrocycle/warm-recovery.json)

4FAB 首案 `gxr_01m8fdh3yjvwr7ph5s` 的第一個通用主鏈 Job 失敗。Lead 改用接受兩份輸入的 Fab 方法；專用 Job `gxj_01m8fdt5txksns88fc` 交付，事件 115 送達「有正佔據率座標的 Cα 位點／已編號位點」50/50。VH 三個環依次為 8/8、10/10、9/9，VL 為 11/11、3/3。Lead 在 128 決定三對環均可對照共有的 IMGT Cα 位點，完整 N/CA/C/O 主鏈仍未知。Lead 親自儲存 `paired-fab-imgt-ca-coverage@1`，其測量步驟接受另一份配對編號及結構，隨後留給 Lead 決定適用範圍。129 結題，共 4 個官方回合。[正式決定](studies/fab-first/DECISION.md) · [兩個 Job、輸入及保存原始調用](studies/fab-first/receipt.json)

線性肽 Run `gxr_01m8fdh3yc0s0mvghe` 已結題，含子模組共 5 個原生回合。兩個一般覆蓋 Job 成功，仍欠末端化學資訊，Lead 追加兩個專用 Job。子模組的部分盤點沒有取得獨立測量。事件 177 將專用結果送達：1UAO 的 18 個構象均為 10/10 胺基酸主鏈，1LE0 的 20 個構象均為 12/12；額外的 NH2 基團使後者已表示成分成為 13/13。Lead 在 193 決定主鏈完整度以具肽鍵的胺基酸殘基為分母，修飾缺載保留未知；195 結題。首案缺 GX 結構化原件取得紀錄，當次前註冊預測仍開放。[正式判斷與限制](studies/linear-peptides/DECISION.md) · [四個 Job／兩份原件](studies/linear-peptides/receipt.json)

1MCP 後繼 Run `gxr_01m8fe32rdt8mqrbaf` 讀取 4FAB 的配方。Lead 原生呼叫 `gx_instantiate_recipe`，採納回傳的圖操作。新 Job `gxj_01m8fe748m25bw7qaq` 的 `recipe_decision` 綁定同一配方摘要 `f425a8f54342460e8b09c0f3cc097eb2dcd0d77117062fa740caa22e18e3cb7b`；實際結構與編號均換成 1MCP，各有新 Artifact 和輸入 SHA。事件 206 將新量測 55/55 送達；VH 為 8/8、10/10、13/13，VL 為 12/12、3/3、9/9。Lead 在 216 決定保留全部六區，以兩份原件共有的域、IMGT 號和插入碼為座標對照條件；尚未直接量測兩份沉積的實際交集。217 結題，共 3 個官方回合。此配方從既有編號對映與座標開始。兩次沉積各自的編號檔已隨發行提供。[後繼正式決定](studies/fab-successor/DECISION.md) · [配方原生調用、真正的 Job、讀數及來源](studies/fab-successor/receipt.json) · [重送同一後繼訊息，新增 0 個 Job、0 個回合](studies/fab-successor/warm-recovery.json)

## 原件取得回執

原研究的 `first-goal.json` 保留首次入場設定。各案隨發行的 `goal.json` 為每份實驗沉積附上機器可核對的取得紀錄。6BEN 衍生對照與 Fab 編號檔繼續作為衍生材料。先前 gzip mmCIF 在來源核驗時無法讀取，修正後以解壓文字核對登錄號和修訂，以實際收到的壓縮位元作為 Artifact 摘要。既有 Goal 輸入大小上限亦用於展開讀取。另起四個准入測試，在原生模型開工前從普通 `research` 入口核對五份實驗材料的登錄號、修訂、論文、SHA 和採樣未知。這四個准入測試沒有模型回合或科學 Job；上述真實研究則各保留自己的輸入與結果。[首次壓縮原件失敗](first-compressed-acquisition.json) · [15 項來源及相容性回歸](source-reader-tests.log) · [四個普通 Goal 的取得事件](source-ingress.json)

```bash
.venv/bin/python -B acceptance/science_assets/modalities/verify_sources.py /tmp/gx-archival-source-admission
```
