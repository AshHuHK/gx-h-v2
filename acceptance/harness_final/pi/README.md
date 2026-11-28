# PI 方法邊界

普通 `bin/gx-p0 research` 建立新 Goal。Lead 在既有開場 `gx_query_catalog(kind=all)` 中提交逐句判讀。GX 記下原句、類型、方法或集合、PI Goal 版本和原生回呼身分。全程限制和未澄清的對象在綁定與新 Job 准入時跟站點及父級範圍取交集。階段與偏好傳給子席，研究順序由 Codex 決定。只有正式更新 Objective 後才可從新 PI 原文解除具名舊限制。現存 Run 讀取原本凍結的選項。

|版本及現場|分階段題的資料庫綁定|全程文獻題的資料庫綁定|
|---|---|---|
|full 4b6aa93，乾淨檢出|9 個可選，拒絕|23 個可選，允許|
|slim 6ec604d|9 個可選，拒絕|23 個可選，允許|
|候選，受控 Lead 判讀|23 個可選，允許|9 個可選，拒絕|

首個 full 工作樹現場另見 baseline-full.json。已安裝酵母包的 `__pycache__` 改變原件雜湊，連帶兩版插件未載入；當時全程題列 20 項、酵母不可綁。以相同提交乾淨檢出獨立驗得上述兩個反例，原始結果一併保留。

候選 candidate.json 保存兩個 Run、事件、原生回呼及輸入／輸出摘要，研究題面只有原始 PI 句子。離線測試由受控 Lead 提交已知判讀，可驗收 GX 強制執行、同一 Goal 不能改寫、子席不能解禁、可讀歧義及原句來源、PI 更新與冷恢復。對自然語言的真模型能力另由官方宿主回執判定。

從產品根目錄執行：

```sh
PYTHONPATH=gx-h:. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python acceptance/harness_final/pi/probe.py --classified
PYTHONPATH=gx-h:. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest -q acceptance.harness_final.pi.test_pi_method_scope.Scope acceptance.harness_refactor.catalog.test_open_discovery acceptance.harness_refactor.modules.test_local_selection acceptance.harness_refactor.routes.test_routes acceptance.harness_refactor.macro.test_macro_recipe acceptance.harness_refactor.integration.test_native_contract
```

同一探針在舊版 checkout 下執行時，使用候選中的 probe.py 路徑並省略 `--classified`。設 `GX_PI_SCOPE_STATES` 可保留每個獨立實驗的 SQLite。上述組合實測 23 項全過，含正常 Job 與中斷恢復。

科學資產與舊版目錄的 21 個鄰接回歸亦已通過。受控 Lead 的中性 Goal 在既有首查明示空條款，保留原測試對翻頁、重播、輸入領域與實際 CPU 計算的斷言。

## 凍結候選的官方首試

`362157a` 上各開一個普通 Goal，只給表中原句。兩案沿現有 ChatGPT OAuth、Clash 及官方 App Server 執行，模型為 gpt-6.2-astra，推理強度 max。兩次首次查詢就留下結構化判讀。原始 code-mode 呼叫、對應 GX 回執與版本均在 official-first-openings.json。

|Run|真實 Lead 首查|同 Run 操作員綁定 `yeast-archive-lookup@2`|
|---|---|---|
|gxr_01m8g0bm5ygyf84cgq|分成兩個階段，各引用相應原句|生效，Run 能力修訂從 0 變為 1|
|gxr_01m8g0bm6e2frd6ws8|文獻為整題限制|SCOPE_DENIED，正式方法清單仍空|

兩次綁定探針在 GX 保存模型原始判讀後執行。部署操作員透過 `bin/gx-p0 capabilities --enable yeast-archive-lookup@2` 測實際准入，方法名稱從未寫入題面。兩案隨後正常暫停，均未派發 Job。全程題被拒前，既有方法註冊程序在全站保存了已安裝的腳本副本與工具描述。兩者均無 Run 或 Task 歸屬，兩個引用在此 Run 的原生回覆與研究上下文中都出現 0 次。此案沒有資料庫讀數、有效決策、研究報告或產物讀取。

舊版相容另有跨程序驗證。在固定部署路徑用 4b6aa93 建立顯式空 scope 的 Run，再原地升級至修補版重開同一 SQLite。方法數維持 0，`gx.harness/2` 沿舊格式完成總覽。見 cold-legacy.json。
