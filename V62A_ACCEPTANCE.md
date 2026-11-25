# GX-H V6.2A 本地研究模組驗收

2026-11-25，產品提交 `3bfca71390b9a0b1215968e54467ab219078a190` 通過 A1–A7。公開 Goal 可委派 `data_inventory@1`，模組可分多次原生決策執行、回收及引用 CPU 量測；Lead 可依報告修圖或結案。案例只使用公開開發資料與本機腳本回答。付費模型呼叫 0 次，供應商 token 與實際美元費用未觀測。V6.2B 負責評估科研效益。[機讀索引](acceptance/v62a/receipts/index.json) · [插件與交接介面](V62A_MODULE_HANDOFF.md)

## 基底與交付範圍

起點是乾淨的 V6.1 發行提交 `c4c5b66a55739b38b34684360810d05f18f9a21a`。開始時查詢到的 GitHub 分支中未見已發布的 V6.1.1。本機 `v6.2a-prerequisites` 的 `5b59213` 和 `c6fc20a` 獨立修復分支監看、小型非 JSON 產物與空閒／待准入狀態；兩個提交都是 A 的祖先。首輪四項檢查重現三個問題。修補後專項 23/23，以及後續等待與恢復檢查 4/4 通過。B 可固定 `c6fc20acdf29f3760c2764bbc80e88c96241e893` 作為單層程式對照。[前置修補收據](acceptance/v62a/prerequisites/receipt.json)

A 在獨立工作樹 `product-v6.2a` 和分支 `v6.2a-local-modules` 開發。正式 A1 與升級驗收另外從乾淨的固定提交啟動。A2–A7 各自保存運行程式清單的摘要。新插件的包摘要為 `e21c7d2cb46e08ed14838a7e7d041a6619a555050092f906db836ee242ebe91c`。本版沿用 GX 的 Goal、圖、租約、預算、Job 及產物儲存；新增的持久紀錄是模組實例、報告與喚醒。V6.1 的原生科研工作樹與 B 的工作樹各自獨立。[實際入口與配置](examples/v62a-data-inventory/README.md) · [增量資料表](gx-h/gx_core/p0/v62_modules.sql)

## 固定提交的七項驗收

| 案例 | 重現的結果 |
|---|---|
| [A1 正常委派](acceptance/v62a/receipts/A1/result.json) | 官方 Codex 0.169.0 使用本地 Responses 腳本，從正常 CLI 開題。模組用兩個真實 CPU Job 取得初始簡報未提供的盤點結果。Lead 在第七次腳本請求收到並引用報告，之後正常結案。 |
| [A2 Lead 與工具交錯](acceptance/v62a/receipts/A2.json) | 分別安排工具在 Lead 改圖之前及之後完成。獨立成果保留，取消後的舊授權不能改圖。顯式反證監看可先於同輪同儕送達。 |
| [A3 雙模組並行](acceptance/v62a/receipts/A3.json) | 兩條 SQLite 連線在寫鎖外準備同一圖頭。第一次提交只有一個成功；另一個模組讀新快照後提交。兩份報告、兩個獨立 CPU Job，費用分別歸屬原 scope。 |
| [A4 模組自建 Job](acceptance/v62a/receipts/A4.json) | 圖修訂結束原短任務。新任務以相同模組 scope、不同租約收回 Job 並發報告，獨立同儕也交付。跨範圍改圖遭拒。 |
| [A5 崩潰後恢復](acceptance/v62a/receipts/A5.json) | 分別在操作完成但尚未交報告、外部 Runner 已接受啟動但 GX 尚未保存回執時退出。恢復核對原 Job、輸入、環境與產物；已確認的工具和 Job 各只執行一次，未知啟動先對帳，新增啟動 0。 |
| [A6 額度與條件派發](acceptance/v62a/receipts/A6.json) | 子預算用盡後由 GX 提交可追溯的 `PARTIAL`，額外模型呼叫 0。完整報告 6,137 bytes，投影 2,048 bytes，實際 Lead 簡報 3,600/4,096 bytes。四個後續候選只有符合量測來源、版本、值與 Lead 採納條件的一項派發。 |
| [A7 相容、隔離、重送](acceptance/v62a/receipts/A7.json) | V6.1 quick 任務經官方回環宿主接回原任務，執行一個 CPU Job。公開測試標記驗證同 Run 越權、跨專案及舊租約新增操作遭拒。委派、報告和喚醒重播各維持一次有效效果。 |

A2–A6 使用本機腳本傳輸控制 GX 正常工具入口的交錯時機。A7 同時使用該傳輸與官方宿主。各案的電腦可讀回執包含具體事件、圖頭、模組、租約與執行次數。額外測試在 12 份報告、三輪、4 KiB 簡報下保住唯一反證及其原始引用；另在普通回報和提早喚醒兩種時序下，確認持舊簡報的 Lead 會取得新決策任務後才可結案。核心專項 32/32、模組與並行邊界 19/19 通過。原 V6 的 45 項和新增 Job 重用 1 項均通過，另有承襲回歸 62/62 通過。這些組別包含重疊覆蓋。[專項檢查與反例](acceptance/v62a/receipts/regression/result.json) · [原測試逐案比對](acceptance/v62a/upgrade/candidate-regression.json)

## A1 委派的實際成本與觀察

同一問題、相同來源與方法，用兩個獨立 Run 比較。2RE9 模型 1 的 A/B/C 鏈主鏈殘基位置聯集有 164 個。2O0O 有 153 個觀測位置，其中 152 個有共同 Cα 可供比較。兩組輸入和結果摘要分別相同，獨立解析器與數值復算對上。兩組結案後均重新開啟，圖頭和帳本相符，新增 Job 與請求各為 0。[原始量測引用、三組擬合與逐次回執](acceptance/v62a/receipts/A1/result.json)

| A1 實測 | quick 單層 | medium 委派 |
|---|---:|---:|
| 官方宿主 turn／本機 Responses 請求 | 3／5 | 4／7 |
| 腳本回報 token／本次帳本 token | 175／175 | 245／245 |
| 實際交付簡報合計，UTF-8 bytes | 18,392 | 16,133 |
| 真 CPU Job／量測 CPU 秒 | 2／0.143 | 2／0.124 |
| 啟動執行至結案，秒 | 3.917 | 4.272 |

委派至提交報告為 2.050 秒。委派增加一個 turn、兩個腳本請求和 0.355 秒經過時間，簡報合計減少 2,259 bytes。每組只有一次量測。固定候選的每次簡報位元組、工具數、原生請求與計量來源都可從 `arms.*.turns` 重建；GPU Job 為 0。較早的一次開發預演中，缺少完整終態計量的一個 turn 產生 65,536 個估算 token，該 turn 的腳本回報為 35。正式收據分欄保存腳本數字和 GX 帳本估算。[預演計量紀錄](acceptance/v62a/receipts/regression/pilot-metering.json) · [官方回環網路驗證](acceptance/v62a/receipts/A1/network.json)

## 啟動與重驗

從 A 的 checkout 進入已部署的 `.venv`。乾淨機器可按固定版本安裝 CPython、Node、Cordis 與 Codex，套用 V6.1 相同的離線安裝命令。工作目錄和 state 放在原始碼樹外。[離線依賴安裝](V61_DEPLOYMENT.md) · [日常 Goal 啟動與 status](examples/v62a-data-inventory/README.md)

```bash
source .venv/bin/activate
python -B acceptance/v62a/verify.py --source 3bfca71390b9a0b1215968e54467ab219078a190
bin/gx-p0 plugin verify org.gx.data-inventory@1.0.0
trial=$(mktemp -d)
bin/gx-p0 start --state "$trial/study" --run-key local-materials \
  --goal 'Which local coordinate data support a comparison, and where is coverage limited?' \
  --research-effort medium --goal-config examples/v62a-data-inventory/grant.json
bin/gx-p0 status --state "$trial/study"
```

要在本機重播完整零付費研究，設 `GX_CLASH` 指向本機 Clash 可執行檔，保持工作區代理與環回配置。在本部署中可使用以下命令。A7 的升級重播需要本機 Git 物件庫中的已發布 V6.1 提交。正式 A1 的請求逐筆紀錄、線路回執和壓縮狀態保存在本機 `../v62a-validation/A1-3bfca71/inventory/{quick,module}/raw/`，各檔的雜湊已納入公開回執。再次執行會產生新的 Run 與回執。[A1 宿主及驗證方法](acceptance/v62a/A1.md)

```bash
export GX_CLASH="$(realpath ../../../clash/clash.new)"
trial=$(mktemp -d)
python -B acceptance/phase2a/regression/loopback.py \
  --clash "$GX_CLASH" --receipt "$trial/network.json" -- \
  "$PWD/.venv/bin/python" -B acceptance/v62a/official_inventory.py --work "$trial/inventory"
python -B acceptance/v62a/interleavings.py --work "$trial/interleavings"
```

## 升級與回退

1. 在舊部署記錄實際 release、state 和 Codex thread store 路徑。停止新准入，等待狀態寫入完成，對整份 state（含 SQLite/WAL、工作目錄、產物與狀態標記）、原生會話及當時程式做同一時間點備份。
2. 在舊部署的實體路徑切換到 A，沿用原 state 路徑。執行 `bin/gx-p0 status --state "$state_dir"`，然後 `GX_CODEX_OFFLINE=1 bin/gx-p0 resume --state "$state_dir" --timeout 10`。持久層以已知合同／DDL 配對作增量遷移。既有 Run 的 workflow、plugin pin 和歷史計費保持原值；舊 Goal 預設 quick，原模組實例數為 0。
3. 這次實際重演三條路徑：V6.1 quick、V6.1 靜態工作流、V6 → V6.1 → A 靜態工作流。三者都經兩次跨程序可寫恢復，原 Job、產物和費用一致。之後逐一在相同路徑還原完整舊快照與舊程式，三者均成功核對。
4. 正式回退前另存升級期間新增的結果和帳本，再恢復成套舊程式、舊 state 與原生會話到原位置。用舊 CLI 讀回核對；如有在途外部 Job 或升級後新增支出，先依原執行身份對帳再准入下一次計算。[升級與回退機讀紀錄](acceptance/v62a/upgrade/result.json) · [重播升級步驟](acceptance/v62a/upgrade/README.md)
