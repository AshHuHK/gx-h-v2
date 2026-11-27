# 兩份獨立 wwPDB 沉積可否共用完整的坐標質控工作流

本例以已公開的泛素 1UBQ 和肌紅蛋白 1MBN 為兩道課題。判別問題是能否把模型 1 的 Cα 回轉半徑標為涵蓋整條已聲明主鏈。PI 要求每一位點均有正佔據率的 N、Cα、C、O，並保留不確定性。兩個登錄號、問題、原始坐標、分母、對應蛋白均各自綁定。歷史檔案的 COMPND 註記含 ENGINEERED:YES，本次只讀既有原件。[事前判據與工作流邊界](preregister.json) · [原件取得記錄、版本與雜湊](materials/sources.json)

## 資產、工作流與科學判斷

- 資產級重用看實際 Job，方法為同一提供者的 `deposited-backbone-qc@1`。以 `structure` 埠傳入當題 mmCIF，產物 `qc.json` 含登錄號、鏈、模型、SHA、Cα 位點數、完整主鏈數及回轉半徑。單鏈、沒有不確定構象及無微觀異質性為此方法的適用範圍。
- 本指標的完整工作流邊界是普通 Goal 入題、目錄選法、實際運算、按具名讀數判定標籤適用範圍，直到 GX 採納決定。配方可保存其中的 CPU 計算及後繼 Lead 判斷點。PI 給第一題的協作者問題屬附加研究任務。判斷工作流有無重用，另檢查父題發布、後題搜尋與實例化、新 Job、讀數和生效決定。子模組分工與最終措辭均可不同。
- `independent.py` 單獨從 PDB 定欄讀 SEQRES、模型 1 的正佔據率 ATOM 行，把 Cα 成對距離平方和除以位點數平方，開平方後與 mmCIF 分析比較。兩格式出自同次沉積，只提供獨立數值實作。較精確的外顯幾何不構成溶液構象或活性證據。[獨立讀取程式](independent.py)
- 校準另以真 CPU 儀器案例驗四種情況。同一在途 Job/遺失啟動回執可恢復原身份；相同條件且原件完整可引用獨立校準；更換實際輸入、來源參數或環境須重新計算；顯式要求獨立重複時產生新的 Job。另一項測試在暫存研究庫移離校準產物再冷重啟，確認重算成功；補回原件後另開獨立重複，再次取得不同 Job。上述是受控回呼和實際本機程序。[九項回歸](calibration/offline.log) · [缺失產物與獨立重複](calibration/missing-result.log) · [針對性測試](calibration/test_missing_result.py)
- 已知不適用輸入 1CLL 含 alternate locations。同一方法在普通 Goal 的真 CPU Job 回傳無法處理不確定構象。這次呼叫由受控 native transport 發出，0 真模型回合，不能把工程失敗解讀成生物學缺失。[失敗 Run、Job 與 stderr](negative-1cll.json)

## 啟動與恢復

在完整產品根目錄執行。已有實測使用獨立狀態 `../gx-science/science-assets-live/archived-qc/state`。新研究請另設私有資料夾；已啟動的研究用原位置和原 Run ID 續行。現場宿主使用既有 Codex OAuth 並只透過配置的 Clash。原始題面均未指定應選的方法名稱。

```bash
study=../gx-science/science-assets-live/archived-qc
case_dir=acceptance/science_assets/reuse
export GX_P0_CODEX="$PWD/vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$study/host" research \
  --state "$study/state" --run-key assets-archive-qc-parent \
  --goal-file "$case_dir/case-a/question.md" --goal-config "$case_dir/case-a/goal.json" --timeout 1500
.venv/bin/python -B "$case_dir/start_b.py" "$study" "$PARENT_RUN_ID"
.venv/bin/python -B "$case_dir/collect.py" "$study/state" "$PARENT_RUN_ID"
```

第一次 A 入場時 acquisition.source 誤為清單，正式配置要求字串。資料庫仍是零 Run、零 Job；修正後使用相同 run key 首次准入。後題以同一條 PI 後繼訊息開題，附自己新材料，重送同一訊息仍指向已開的 Run。[首次入場錯誤](first-admission.log) · [A 題面](case-a/question.md) · [B 題面](case-b/question.md)

## 現場回執

A 的目錄選擇、CPU 計算、保存配方與結題均由真實 Lead 完成。兩個原生研究員工作區間實際重疊 111.079 秒及 60.233 秒。第一份檔案研究的報告由 GX 標為 PARTIAL；兩項原文摘錄有被 Lead 引用。Lead 在事件 199 根據另一位研究員的簽署報告開始補查，後繼研究員的原件覆核在 263 簽署、264 送達。結題決定 294 引用了兩份研究員簽署的報告，296 關閉。計算 Job 本身歷時 0.352 秒，與兩位研究員的真回合重疊。第一次失敗與後繼補查都保留於同一個 Run。[A 科研決定](case-a/DECISION.md) · [完整事件、讀數與拒絕記錄](case-a/result.json)

B 首次目錄查詢把 A 保存的配方列在首位，檢視後以 1MBN 自己的原件實例化。它先給了一個尚未開立的裸分支名，舊展開回執回傳了當時無法直接提交的節點操作。Lead 自行改用 `local:myoglobin`，取得開分支欄位與資源提示，後續提交後 GX 才派發計算。這次現場發現已改進 v2 配方預覽；缺少的裸分支在首次預覽即回傳具體修正，現有有效分支與舊 v1 預覽沿用原格式。相同新、舊回呼在唯讀同庫重放，修改資料列為零。[兩個真回呼與修後比對](branch-preview.json) · [修前失敗](first-recipe-preview.log) · [修後回歸](recipe-preview-regression.log) · [正常 Goal 覆驗](recipe-preview-normal-goal.log)

| 課題 | Run／已完成新 CPU Job | 模型回合 | 四原子完整／聲明位點 | Cα 回轉半徑（Å） | 讀數送達 → 結題決定 → 關閉 |
|---|---|---:|---:|---:|---|
| A · 1UBQ 泛素 | `gxr_01m8fddc2b6pjbf83p`／`gxj_01m8fdfhdwep1qw3h0` | 13 | 76／76 | 11.492894876 | 286、287 → 294 → 296 |
| B · 1MBN 肌紅蛋白 | `gxr_01m8febe21hn46darv`／`gxj_01m8fefs2bw2c25tp3` | 6 | 153／153 | 15.127861445 | 429、430 → 438 → 440 |

B 在事件 419 引用本題原件與四個測量量，暫採整鏈模型標籤並等自己委派的位點覆核。最終採納研究員的具名報告與本題輸出，保留沉積原件對 Gln8 Cα 手性的警示。該警示限制立體化學解釋，四個所需的主鏈原子在該位點均有坐標。兩題的 Cα 位點數均等於各自分母，且與另一份格式、獨立實作的全精度數值差均為 0.0 Å。本次證明兩個不同原件上重新選用並執行完整的有界質控流程；兩題的覆蓋結果都支持完整標籤，尚無真模型遇缺失位點而降級標籤的第三個觀察。[B 的正式決定](case-b/DECISION.md) · [B 完整科學記錄](case-b/result.json) · [跨題欄位、回合、時間與產物引用](field.json)

A、B 均保留了六次被 GX 拒絕的草稿。場測持續期間工作樹有並行開發，A 准入時基線為 `595649e`，依提交時間 B 准入點最近的提交為 `0c514ba`；兩次實際任務的合約、工作流、政策、方法、插件和材料摘要均可在回執檢查。B 的真模型自行修正發生在 `9c69878` 的預覽改善之前。負對照 1CLL 的格式適用性錯誤仍獨立保留。正式科研 A、B 在既有 OAuth 訂閱執行，啟動 2、結題 2、滿足此次事前質控驗收 2。

離線複驗可直接使用散布的原始檔及回執。以下命令重算兩份 PDB 檔、驗證原始 mmCIF 摘要、新 Job、配方、具名讀數、原生研究員交接和結案回合，不啟動模型。

```bash
python3 -B acceptance/science_assets/reuse/verify_field.py
```
