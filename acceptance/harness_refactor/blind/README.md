# 三臂公開配對盲評

題目是替食品酵母資料庫覆核一個已發表測量。BRENDA 快照中的記錄、工作表上待核的第二個數，以及之後到達的期刊檔案投影，三臂逐位元相同。委託人想知道可以發表哪些欄位、哪些仍然未知。此例只評原始資料的歸屬和讀法；生成新分子和預測模型性能的計分分母皆為零。[題面](packet/question.txt) · [初始附件](packet/initial.json) · [遲到原文投影](packet/late.json) · [摘要與公開原件](packet/sources.json) · [逐位元重建回執](source-verification.json)

| 條件 | 實施方式 |
|---|---|
| 對照 | 原生官方 Codex 直接使用自己的 App Server、subagents 與 thread/resume；舊版固定在 50d31f5；候選使用事前登記的完整 commit。兩個 GX 臂均由其自己產品的 `bin/gx-p0 research` 開題與恢復。 |
| 相同科學入口 | 都能讀 E1，在同一時間點收到 E2。場內無網路查詢或計算 Job，兩個 GX Goal 顯式固定空方法範圍；原生的科學工具只讀 E1。各自原有的組織、狀態和協作工具仍依其產品運作。殘餘差異包括 GX 會顯示其管理工具與目錄，單臂僅有一次公開題。 |
| 資源 | 每臂相同官方模型、max effort、900 秒整臂牆鐘，時窗跨兩階段連續累計。現有 ChatGPT OAuth、Clash loopback、零追加付款、零外部科學 Job。裁決使用另開的獨立同模型官方會話。 |
| 中斷事件 | 讀到初始 E1 的實際科學內容才觸發一次 SIGKILL，包括屬於該驅動的 App Server 子進程。僅列目錄、看見附件名或讀取錯誤不能觸發。三臂還須在官方 rollout 找到同一 thread、turn、call 的成功完成事件，且返回內容逐欄等於 E1；只靠回調已寫入資料庫不足以觸發。記下原案／原生 thread、回合和外部作業數。恢復時僅一次遞交 E2。 |
| 打分 | 固定菌株、底物、條件、蛋白歸屬、科學定位、反證處置和限制共七欄；每份完成報告分母 7。受控中斷及既定 E2 送達均為設計內操作。漏掉介入時點、逾時或未結案照實登記。無資料、未觀察到的機會與數值 0 分列。 |
| 盲法 | 每臂保留實際最後結論、理由和局限，另列遲到材料送達後的已提交決策原話。評閱者在隱去時間的原話中標註首次採納點，解盲後再對回原始時間。去除 harness 識別碼，保留科學名稱、數字和原件定位。三臂終結後隨機分派匿名標籤；每份交給一個新且無工具的評閱會話，裁決者收不到分支、身份、成本、執行軌跡與對照答案。收齊裁決回執才可解盲。 |

[預註冊](preregistration.json) · [計分準則](rubric.json) · [官方零推理入場檢查](preflight.json) · [離線針對性測試](tests.log)

## 重現

在完整研發樹根目錄啟動，也可以用 GX_P0_CODEX 指向部署已安裝的官方二進位檔；啟動前仍須與發行版 SHA 相符。原產品狀態、native sessions、受限對照資料及匿名解碼表放在自己指定的私有工作區。需要 epoch 內首次啟動的新研究可用新空目錄；續行保留已綁定的原工作區。若硬中斷後不確定 native turn 是否已送出，保留 launch receipt 並用原 rollout 對帳；請勿刪去 launch receipt 另開試驗。

```bash
case_dir=acceptance/harness_refactor/blind
study=../harness-refactor-field-private/three-arm-first
candidate_tip="$(git rev-parse HEAD)"
baseline_dir=../harness-refactor-baseline-50d31f5
# 首次部署才建基線工作樹
git worktree add --detach "$baseline_dir" 50d31f5
.venv/bin/python -B "$case_dir/probe.py" ../harness-refactor-field-private/check
for arm in stock baseline revised; do
  .venv/bin/python -B "$case_dir/trial.py" "$study" "$arm" \
    --baseline "$baseline_dir" --candidate "$candidate_tip" &
done
wait
.venv/bin/python -B "$case_dir/score.py" "$study" export
.venv/bin/python -B "$case_dir/score.py" "$study" judge
.venv/bin/python -B "$case_dir/score.py" "$study" summary
```

`summary` 逐臂輸出原始狀態、接受回合、SQLite 事件、來源讀取、E2 交件、官方 Lead 會話確實收到同一完整文件、獨立盲評指認的首次採納時間、至正式結題的時間上界、中斷後新增的外部 Job 數、每欄獨立裁決與其官方回合。此設計檢驗零外部科學作業場合的中斷恢復。非零在途 Job 的對帳與重送歸第二批的故障測試。原文篩選、菌株釋義或其餘工作表之正確性只在本題明定的範圍內解讀。

要獨立重製科學材料，在部署的既有 Clash 路徑取得來源記錄所列的完整 PMC JATS，將其作為 `ARTICLE_XML`。BRENDA 快照保持唯讀，兩個資料源均由內容摘要固定。

```bash
.venv/bin/python -B "$case_dir/build_materials.py" \
  gx-h/var/science-assets/yeast-archive/kinetics/brenda2026.1-sabio20260920-v1.sqlite "$ARTICLE_XML"
.venv/bin/python -B -m unittest -v acceptance.harness_refactor.blind.test_protocol
```
