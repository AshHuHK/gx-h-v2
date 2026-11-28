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
