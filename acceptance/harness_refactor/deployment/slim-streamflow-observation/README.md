# 精簡版獨立研究

普通 `research` 入口接收一個新問題及一份 USGS 原始日流量檔。問題詢問單站相鄰日變化，原始資料還含另一站的讀數。唯一交給 Lead 的材料是 `usgs-daily.json`。數值複算和驗收條件凍結在 `frozen-reference.json`，保留在驗收端。

在已設定官方 Codex 訂閱與 Clash 的精簡部署環境，以該部署的 Python 和下列普通入口開題。`case_dir` 指向本材料目錄，`study_dir` 指向產品以外獨立的研究目錄。

```bash
bin/gx-p0 research --state "$study_dir/state" --run-key streamflow-observation-slim-first \
  --goal-file "$case_dir/question.md" --goal-config "$case_dir/goal.json" --timeout 900
```

已啟動的個案用同一 Run ID 接回，保留原始回合、Job 和結論。

```bash
bin/gx-p0 research --state "$study_dir/state" --run-id "$run_id" --timeout 900
```

## 正式結果

精簡版從普通入口只開一次 Run，`gxr_01m8frjb04ng1edrvg` 以現有官方訂閱完成一個 Lead 回合。Lead 親自讀取 USGS 原始 JSON，提交後在事件 31 生效，在事件 33 封存。4 月 1 至 2 日降幅為 20.2 ft³/s，次日降幅為 14.8 ft³/s。這三天的下降無法建立長期趨勢。原始日值足以供 Lead 直接計算，本題派發 0 個 Job。

[原始報告](REPORT.md) · [結題決定](DECISION.md) · [官方回合、讀取與提交](slim-live-receipt.json) · [獨立數值核對](science-check.json)

冷重開同一 Run 執行 `export_closed`，新增 0 個宿主回合、0 個 Job、0 個 Run 事件。報告與決定的 SHA-256 均與首次匯出相同。結構化結果記錄了本次匯出命令，所以其檔案摘要更新；將該次呼叫欄位還原後，整個 JSON 的雜湊即與首次匯出一致。[冷重開與雜湊](cold-export.json)
