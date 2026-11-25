# V6.2.2 公開批次與離線演練

使用同一個正式 Goal 入口比較兩種委派授權。每個原始格由官方 Codex App Server 讀取研究簡報、透過 GX 工具選擇方法或開模組、提交圖修改並接收真實 CPU Job 結果。此版本的公開研究者由本機 Responses 腳本驅動。模型名與推理設定可在原生請求核對；這些請求的計量來自本機替身。公開真模型仍為 0/4，歷史私有配對仍為 0/2。

## 執行

在已安裝鎖定資產、官方宿主與依賴的候選 checkout 根目錄執行下列命令。兩個結果路徑需為倉庫外尚未存在的絕對路徑。CLASH_BIN 指向本機 Clash 執行檔。包裝器在只有 loopback 的網路命名空間啟動 Clash 與離線宿主，留下 network.json。原始 Goal、工具、Job 和研究狀態均從產品入口執行。

```bash
bash acceptance/v622/public/run-public.sh "$SCRIPTED_OUT" "$CLASH_BIN" standard
bash acceptance/v622/public/run-public.sh "$FORMAL_OUT" "$CLASH_BIN" controls
```

`standard` 沿用固定四格順序與歷史腳本的研究選擇，啟用共同的新版 deep 證據政策。`controls` 使用獨立系列和同一正式執行器，研究者依序選擇直接取數、介入後才委派、根據不足覆蓋提早結束，以及在低覆蓋時自稱改路卻執行原方案。驗收分開記錄每格是否接獲可用量測、科學判斷、生效的行動、委派鏈、各角色請求與實際支出。

`batch.py` 負責凍結授權、計量與交付介入，會等第一個 Run 層計畫正式提交後才派送資料。研究選擇在離線研究者或模型端。批次保留當初的格位、Run key、截止時間及實際結局。評分者先在 opportunity-lock.json 固定已投遞讀數，再讀取後續行動；每格完整性和結算通過後才繼續。科學負例照原狀保留。上限、來源或科學完整性錯誤會暫停後續格並保存原因。臨床或生物學外推不在這項幾何覆蓋測試的範圍內。

若控制程序意外結束，待舊程序退出並釋放批次鎖後，從原先的 checkout 和同一份結果目錄繼續。例如：

```bash
.venv/bin/python -B acceptance/phase2a/regression/loopback.py \
  --clash "$CLASH_BIN" --receipt "$FORMAL_OUT/recovery-network.json" -- \
  "$PWD/.venv/bin/python" -B acceptance/v622/public/batch.py \
  --host offline --suite controls --work "$FORMAL_OUT/run" --resume
.venv/bin/python -B acceptance/v622/public/rate.py --work "$FORMAL_OUT/run" --stage opportunity
.venv/bin/python -B acceptance/v622/public/rate.py --work "$FORMAL_OUT/run" --stage outcome
.venv/bin/python -B acceptance/v622/public/check.py --work "$FORMAL_OUT/run"
```

原案例已失敗、逾時或帳務暫停時，`--resume` 保留該案例及剩餘未開格。若在提交介入後、寫入本機回條前中斷，會重送同一個已保存的派送操作，以 GX 原始收據對帳。離線 host 只接受本機實測替身；`--host live` 會在登記前回傳退出碼 2。真實供應商現缺可執行的整次請求 token 上界和這一批的獨立授權。

## 複核回執

- `run/registration.json`、`runtime-pins.json`、`custody/` 含實際 checkout SHA、程式逐檔雜湊、材料與轉換、兩臂授權及事前協議。`protocol.json` 新版系列獨立，舊四格協議、原分母和既有回執仍可從原目錄回播。
- `run/NN/state` 含原 SQLite 和不可變產物。`codex/sessions`、`raw/wire.json`、`raw/commands.json`、`raw/requests.jsonl` 記錄實際原生投遞、呼叫與結果。`assignment.json` 是 GX 派送回條，`assignment-request.json` 保留其原始操作參數。
- `run/scores.json`、`ratings/NN/` 和各格的 `assessment/` 連接獨立重讀原檔、投遞游標、被引用證據及行動的實際生效事件。`capability-parity.json` 比較真實方法卡與工具讀取，`verification.json` 核對已登記的固定腳本正、反例。
- `run/funding.json`、`funding.sqlite` 逐請求保存原子預留、完整本機替身收據、計費模型、分角色合計及未知曝險。每格 `summary.json` 包含冷恢復狀態摘要及模組報告重送的結果。外部付費呼叫數為零；實際外部 token、單價及美元欄位保持空值。
