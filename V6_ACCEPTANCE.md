# GX-H V6 驗收紀錄

2026-11-25。本次交付涵蓋 Agent Kit 的 M0、M1、M2。以 `e02df99757686982744813e5de745e86ef12e0a3` 為基準；複跑的工程測試、第二次真實研究與第二組盲測使用固定的 `fe7ff695a7328c198ffb5bb90b50cc2391f6f397`。後續改動包含 Goal 開題時沿用 JobService 的能力准入檢查，以及按實際送達的新觀測選擇匯出窗口。前者另以可執行能力及兩種無效授權實測，後者以原始 Run 離線重播取得相同的獨立摘要。部署入口、樣例授權、離線對帳與回退步驟在 [V6 部署說明](V6_DEPLOYMENT.md)。

| 里程碑 | 實際驗收 | 範圍 |
|---|---|---|
| M0 基準與復用 | 固定版靜態蛋白流程在兩個新 Run 各執行 4 個 CPU Job，冷重開各自新增 0 個。核對過 33 個 V5 案例的去向，重新取得並校驗 27 份重用資產。 | [基準、版本及原件](acceptance/v6/m0/README.md) · [取用清單](acceptance/v6/reuse/README.md) |
| M1 Goal-only 產品入口 | `gx-p0 start` 在尚無 workflow 或圖頭時即可持久建題。官方宿主呼叫採納第一個圖和派發意圖，JobService 在交易提交後啟動工作。13/13 個正式工程案例通過，另有 62/62 個受影響的既有測試通過。 | 圖頭競爭與冪等、Goal／lease 變更、冷恢復、既有靜態流程、並行分支、計量及匯出竄改負例。[工程收據及重跑方式](acceptance/v6/engineering/README.md) |
| M2 真實研究 | 兩次獨立開題均經由原生 Codex 讀取實際產物、保存有條件的主張並結題。各完成 5 個 CPU Job，冷重開後身份、結論與提交筆數可核。第二次通過原套件和產品兩套匯出檢查。 | 一個 2RE9 沉積結構、已有的三個方法、同一種模型。原始位元組與九組數值複算另由獨立審閱程序核對。[實驗原件覆核](acceptance/v6/reviewer/REVIEW.md) |
| M2 匹配盲測 | 第一組通過兩案，中性刺激多派了兩個 Job。預先固定同版新試驗，支持、相反與無關三案的科學結果均符合 rubric。產品檢查器通過第二組，原套件檢查器仍拒絕其中同一 turn 新產生的引用。 | 三個獨立會話，起始材料和額度相同。干預中的數字是合成材料，模型呼叫和後續計算是真實的。保留兩組結果和兩種判定。[第一次](acceptance/v6/reviewer/controls-first.json) · [複試](acceptance/v6/reviewer/controls-repeat.json) |

## 從原始座標到已執行的停止決定

1. PI 從 Goal 入口授權 2RE9 原始 mmCIF、三個版本化方法和硬額度。凍結版 Run `gxr_01m8871dsn80ypeaz1` 起初只有一個待決任務。Lead 收到題面，可透過本 Run 的工具取得材料。其會話工作區與工程包、前次答案和 reviewer 目錄分開。
2. Lead 首次採納 `amend`，選擇座標盤點、三種選取方式的幾何擬合和獨立數值複核。五筆工作實際完成。第二次決策在先收到盤點時以 `resolve` 保存覆蓋資訊及有限主張，讓在途工作繼續。
3. 共有 Cα 及共有主鏈均覆蓋 164 個位置，三個鏈對兩種選取間最大角差 0.06439°、RMSD 差 0.03988 Å。兩者均在既定界線 0.5° 與 0.15 Å 內。有序骨架只有 49/164 個位置，未達至少 60 處及 60% 的雙門檻。獨立程序從原座標以 SVD 重做九組擬合，最大差異為 8.53×10⁻¹⁴° 和 1.33×10⁻¹⁵ Å。[複算明細](acceptance/v6/reviewer/geometry-02.json)
4. 新觀測到齊後，Lead 把覆蓋層面的 `limited_support` 修訂為區分選取方法的 `mixed`，留下有序子集的限制，並提交 `close_run`。圖、主張和停止各有正式回執。GX 在事件 131 記下科學結題，外層 Run 為 `STOPPED`；冷重開後沒有新增工作或收費。獨立摘要對應匯出的 SHA-256 `7e9a89cd06e3cfa91d89b3ebbedc15ad698d8963b071cc5da203869db4f91799`。[原生來源對照](acceptance/v6/reviewer/observed-02.json) · [兩套檢查器的退出碼](acceptance/v6/observed-run-02/anchored-verify.json)

這次實證支持 Lead 自行選法、保留待決問題、看過反證後限縮主張及結題。這是一份沉積晶體模型；三組鏈對共享座標。跨蛋白泛化、溶液態分布、功能效應、第二模型或第二提供商均待另行驗證。

## 重用、重評、重跑與費用

沿用原版 Codex App Server、GX Driver／Engine、Task／Lease、JobService、Intent、Artifact、outbox 與計量所有者。蛋白插件沿用已發布的坐標盤點、幾何擬合及獨立數值複核程式。兩次新研究有各自的可見性與額度，皆在新 Run 重作原始觀測。單一 Run 的冷重開直接重用其產物。僅改數值門檻可用既存量測重新評分；M0 的 0.01° 敏感性檢測就是這種重評。三種原子選取改變輸入映射，因此 Lead 選擇真實重新擬合。靜態參考、盲測干預和完整原報告留在工程與審閱側。[插件與版本](acceptance/v6/m0/plugin-smoke.json) · [基準的判據重評](acceptance/v6/m0/2re9-fresh.json)

| 試驗 | 原生模型呼叫 | 提供商確認 token | 另列估算 token | CPU Job | 實測／入帳 CPU 秒 |
|---|---:|---:|---:|---:|---:|
| 真實觀測 01 | 3 | 353,579 | 0 | 5 | 1.083 / 5 |
| 真實觀測 02 | 3 | 259,718 | 0 | 5 | 1.020 / 5 |
| 首輪三個盲測 | 6 | 130,952 | 196,608 | 9 | 0.738 / 9 |
| 複試三個盲測 | 3 | 156,191 | 0 | 7 | 0.484 / 7 |

合計 15 個原生呼叫、900,440 個已確認 token。首次盲測中另有三個已中斷的後續呼叫，GX 各估算 65,536 個 token，仍有低估可能。各表計算 CPU 用量按每個 Job 進位。以上 GPU Job 為零；提供商未給美元帳單。早期原件及再對帳結果保留各自的時間點。[首組帳目](acceptance/v6/controls/runs/first/meter.json) · [複試帳目](acceptance/v6/controls/runs/repeat/meter.json)

## 驗收邊界與可重放性

- 中性盲測首次失敗後，修改的是所有 Goal 通用的決策簡報，包含既有承諾、上次理由和觸發複議的條件。複試三案預先固定每個世界一次付費呼叫。支持與相反材料分別產生有限支持與混合主張，各有兩個幾何 Job。中性改名維持原主張，新增零個 Job。[事前配置](acceptance/v6/controls/runs/repeat/preregistration.json) · [逐案成本與結果](acceptance/v6/controls/runs/repeat/results.json)
- 原套件只接受 turn 開始時已存在或屬於當次輸入觀測的引用。首次真實研究與兩組盲測包含同一 turn 內先產生的主張引用，因此原檢查器輸出 `invalid evidence citation`。產品檢查器另外核對官方工具呼叫、原回執、產物位元組與採納前的順序。第二次真實研究的匯出本身同時滿足兩套規則。[檢查器與負例](acceptance/v6/EXPORT.md)
- 兩輪付費盲測都保留了獨立引文 Job 的身份、原結果及單筆提交費用。獨立檢查 process 時間戳顯示，引文工作在評分決策到達前已自然結束，另以正式 GX 採納路徑和腳本決策補測實體並行。採納停止幾何分支的交易前後，觀察到同一引文工具 PID 正在執行，之後成功結束；冷重開仍是一筆 Job 和一筆預留，消耗零模型 token。[盲測原始時序](acceptance/v6/controls/runs/repeat/citation-job-lifecycle.json) · [實際並行回執](acceptance/v6/controls/runs/repeat/physical-overlap.json)
- 原件的保管與驗證由同一受信任主機上的獨立程序完成。分開保存 detached anchors、兩個真實研究的 SQLite 一致備份、Job 輸出和可重播的官方工具記錄；研究 Lead 無法從其授權材料取用這些記錄。[Run 01 封存索引](acceptance/v6/observed-run-01/operator-archive.json) · [Run 02 封存索引](acceptance/v6/observed-run-02/operator-archive.json) · [重放步驟](acceptance/v6/LIVE_REPLAY.md)

凍結版另通過原有公開 REG01 回歸路徑，涵蓋所選閘門的跨程序恢復、分支失敗、原生路由、性能與 Job 測試。其 Python 主測試共 526 項，522 通過、4 項按環境條件跳過，P0 測試 129 通過，Node 測試 2 通過。正式 verifier 重驗了 217 份原始記錄。[計數與跳過原因](acceptance/v6/engineering/regression-fe7ff69.json) · [REG01 執行摘要](acceptance/v6/engineering/runs/v6-legacy-fe7ff69/summary.json)

正式 `v6-exports` runner 以分開保管的兩份摘要重驗第二次真實研究及三案複試，兩個 Step 皆通過產品檢查器。[正式匯出回執](acceptance/v6/engineering/runs/v6-paid-exports-fe7ff69/summary.json)
