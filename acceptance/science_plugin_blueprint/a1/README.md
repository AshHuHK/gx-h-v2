# A1　從一般科研目標選擇可用方法

兩題、儀器記錄與判準在編寫方法、調整索引和啟動研究之前由插件作者封存。問題只談兩條水道的同時觀測；兩次研究分別擁有私有狀態庫及正式 Codex 會話。APR-17 提供帶時間和有效旗標的原始流量，APR-18 只有站點總量。兩份皆為構造儀器資料。APR-18 的來源欄明說它是邊際覆蓋的反例，且記錄缺失的欄位；兩題都沒有給出方法名、應有數值或預期行動。因此負例檢查的是 Lead 面對明示的資料局限時有沒有捏造結論。

## 安裝

兩個獨立包位於 `examples/plugins/hydro-concurrent` 與 `examples/plugins/hydro-marginal`。作者使用標準 plugin manifest、`ctx.capabilities.add_manifest`、具名輸入與具有量綱的輸出。每個包在 labels 中加入 `gx_capability_collections=cpu_analysis`。研究 Goal 只選 `capability_collections=["cpu_analysis"]`；正式研究的選法、授權範圍內啟用、派工與收件沿用 GX。

```sh
bin/gx-p0 plugin digest examples/plugins/hydro-concurrent
bin/gx-p0 plugin install examples/plugins/hydro-concurrent --sha256 b949664445594f47337a80cc5c52cdc7f9640d3afde39d5c70f37843bd3e7729
bin/gx-p0 plugin verify org.gx.hydro-concurrent@1.0.0
```

## 執行與唯讀核對

在產品根目錄執行。將 `WORK` 指向本次驗收的私有目錄；兩個案例的 `run` 各用相同的首測 run key。復原時重用同一路徑，讀取已結題的案子僅重匯出報告。只做唯讀核對可跳過所有 `run` 命令。

```sh
WORK=../release-acceptance/science-plugin-blueprint/a1
python -B -m unittest discover -s acceptance/science_plugin_blueprint/a1 -v
python -B acceptance/science_plugin_blueprint/a1/operate.py prepare positive "$WORK"
python -B acceptance/science_plugin_blueprint/a1/operate.py prepare negative "$WORK"
python -B acceptance/science_plugin_blueprint/a1/operate.py run positive "$WORK"
python -B acceptance/science_plugin_blueprint/a1/operate.py run negative "$WORK"
python -B acceptance/science_plugin_blueprint/a1/export_first.py "$WORK" /tmp/a1-original.json
```

`prepare` 建立兩個普通 GX 發行環境，用 `plugin digest/install/verify` 安裝指定套件，且都註冊一張僅供參考的舊卡。正例部署有兩個方法；反例部署只有按站點加總的方法。兩個部署共用正式發行版的 Codex 執行檔，驗證 lock 中的摘要，沿用 Clash 和既有 ChatGPT OAuth。每次啟動前保存的 GX 核心摘要與插件回執位於各部署的 `release/a1-deployment.json`。每次首次研究的原始資料庫位於 `$WORK/<case>/state/gx.db`，完整官方會話位於同一案例的 `official/codex/sessions`。可分享的回執由唯讀索引程式抽取，保留原事件與 Run／Job／Artifact ID。

在額外的控制情境中，正常 GX 工具在同一個 Run 處理三次不同的科研更新，再重開資料庫、重送相同查詢。測試檔為 `acceptance/science_plugin_blueprint/test_catalog_survey.py`。這與兩個首次盲題的真模型行為分別計證。

## 首測結果

| 原始 Run | 實際研究與生效決定 |
| --- | --- |
| 正例 `gxr_01m8f5fp86mpxmg73p` | 三個正式 OAuth 回合。首回合的 #19 將同時段積分與邊際積分分列第一、第二。Lead 自行啟用前者，#35 派出唯一 CPU Job `gxj_01m8f5hjhntmap80qe`。原件產物 `gxa_01m8f5hjyspqtvqpz0` 給出同時觀測 85 分鐘，West 16,470 L，East 11,730 L，差 +4,740 L，保守區間 [+3,330, +6,150] L。#70、#71、#72 送達七項具名讀數。#82 採用優先核查 West 的建議，#83 結題。兩處缺測使全時段排序仍未知。逐分鐘獨立復算符合凍結容差。 |
| 反例 `gxr_01m8f5fmqqnkcgep1x` | 一個正式 OAuth 回合，零 Job。首回合並行查詢的頁長超限，Lead 自行調整，同回合的 #19 返回邊際方法及僅參考卡。兩站各覆蓋 75 分鐘於 120 分鐘窗，故共同時間介於 30 與 75 分鐘。9,000 L 和 8,100 L 是不同時間範圍的總量。#30 以 `INSUFFICIENT_DATA` 結束並暫緩單站追查，#31 正式結題。要求補充同步有效時段及流量或同時段雙站總量。 |

兩題均只有一次 Run。均通過各自的事前判準。完整的 [正例決定](positive/DECISION.md)、[反例決定](negative/DECISION.md) 和 [首輪回執](first-attempts.json) 可對照。正常關閉後重入兩次分別增加零 Job、零模型回合。[恢復回執](recovery.json)

正例有七項、反例有四項工具層拒絕，包括在 JSON 儀器材料上使用要求已註冊資料庫原件的讀取器、缺少來源範圍或具名讀數的主張草稿，以及反例的超限目錄頁長。保留原始拒絕及同一回合自行修正的結果。正式決定沒有遭到拒接。正例曾在 Job 完成與首個結果簡報之間維持原計畫一次，然後在讀到具名產物的下一個決定完成判斷。

負例的來源說明明示資料曾做邊際彙整。這個首次實驗確認了兩題上的方法發現、執行與資料不足判斷，樣本量不足以估計新題成功率。插件由與 GX 核心修改者不同的實作 agent 撰寫；這裡沒有宣稱已有外部獨立人體作者的實測。

首次兩個 Run 的首份 Lead 簡報在 capability_view.available_options 中已列出已裝方法短名。題面沒有給方法名，Lead 也確實從科學需求發出非精確目錄查詢並比較候選；這項驗收能支持「在已授權候選中自主選用」，對完全沒有短名提示的開放發現仍需另外驗證。準入至首個合格產物的耗時、首五排名、實際拒絕分母與重複作業見 [唯讀指標](metrics.json)。
