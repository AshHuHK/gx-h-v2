# 第三份沉積的盲測入口

GX 使用普通 Goal、正式的比較提案、材料指派與 CPU Job。保管者先凍結篩選及位置映射。Lead 起初只見研究題、盲碼、適用域、先導研究摘要、方法選項及成本。它自選區段、遮罩、端點，並以自己的語言說明預期與兩種結果各自支持的報告行動。[科研原題](SCIENCE_GOAL.md)

開封採用現行 Goal 版本最早的合格 Run Lead 決策。GX 核對決策、採納回執、方法版本、卡片引用與盲碼。同一個材料指派交易交付原始座標和對齊檔。重送會找回原本的兩筆指派。新的正式 Goal 版本或已關閉的分支不能沿用舊承諾。啟動後採用絕對時鐘，已有 Run 的重啟會先讀取原始用量。正式窗口為 1,800 秒新工作、360 秒排空、120 秒對帳。

## 以公開查詢結果登記

在發行 checkout 根目錄執行。目標須是 checkout 外的新目錄。

```bash
py="$PWD/.venv/bin/python"
w="/absolute/path/to/new-real-study"
"$py" -B acceptance/science_execution/study/operate.py seal --work "$w" \
  --custody acceptance/v624/custody/receipts/eligibility.json
"$py" -B acceptance/science_execution/study/operate.py operate --work "$w"
```

本次資格檔為 `NO_ELIGIBLE`，上述命令交回 `NOT_ENROLLED`。此結果有獨立分母，模型呼叫、科研開封和計算均為零。[保管者候選與排除依據](../../v624/custody/README.md)

合格系列的正式命令為 `operate --work "$w" --diagnostics "$closed_diagnostic_batch" --approval "$successor_pi_receipt"`。它核對同一候選版本、帳戶、診斷鏈、保管證據與研究授權。原生宿主只使用既有 ChatGPT OAuth。進程中斷時，同一命令保留原 Run 與期限；`reconcile --work "$w"` 只讀原用量。原帳務仍有缺口時，狀態為 `HELD`。

## 用透明合成座標重跑工程驗收

```bash
py="$PWD/.venv/bin/python"
w="/absolute/path/to/new-synthetic-study"
clash="/absolute/path/to/clash.new"
"$py" -B acceptance/science_execution/study/operate.py seal --synthetic --work "$w"
"$py" -B acceptance/phase2a/regression/loopback.py \
  --clash "$clash" --receipt "$w/network.json" -- \
  "$py" -B acceptance/science_execution/study/operate.py operate --work "$w"
PYTHONPATH=gx-h:. "$py" -B -m unittest \
  acceptance.science_execution.study.test_commitment \
  acceptance.science_execution.study.test_mapped_method
```

此路徑使用官方 Codex 二進位與本地確定性 Responses。輸出的 `study-receipt.json` 標示 `SCRIPTED_SYNTHETIC`，包含原決策與 turn、原件與映射摘要、Job manifest、完整表格實收回執、四個變化量、獨立複算與實際生效的後續動作。失敗和部分進展記入 `study-progress.json`，保留原始資料庫與 rollout。PI 可讀的表格位於 `report.md`。只讀同一張表會沿用已完成的 CPU 計算。
