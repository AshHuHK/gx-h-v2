# GX-H V6.2.2 工程與公開離線試點

| 判定 | 狀態 | 依據 |
|---|---|---|
| V6.2.2 工程候選 | **PASS** | 以凍結提交跑過正常 Goal、Job、來源、報告、正式發布及結題路徑，並核對舊 Run 冷恢復和回退。[機讀索引](acceptance/v622/receipts/index.json) |
| V6.2C 公開入口離線驗收 | **PASS** | 已安裝的官方 Codex App Server 連接本機 Responses 替身。固定四格與行為對照各自登記、實際執行並從資料包重評。[固定四格](acceptance/v622/receipts/standard/verification.json) · [行為對照](acceptance/v622/receipts/formal/verification.json) |
| V6.2C 付費就緒及授權 | **BLOCKED** | 供應商尚缺涵蓋整個請求的可驗硬上界，本批也未獲付費授權。正式入口在建立批次前以退出碼 2 結束。外部付費呼叫 **0**，公開真模型 **0/4**，原未見配對 **0/2**。[付費入口回執](acceptance/v622/receipts/funding/live-preflight.json) |

簽收來源為 `a0d167cc34a51b2c687e038510372c84971616a0`，基線為 `e5b7ee977ec33854362bc2440d8f9249464aed20`。先在舊版正常發布流程留下 R1–R3 的錯誤結論（`c79cc6e`）和其餘對照（`c898245`），再修產品（`f089ccf`）。最終提交補齊三份測試，分別驗證舊政策的原語義和新政策的修正。各候選的分派、失敗及評分獨立保存，以下數字只取最終提交。舊版原始索引仍為 23 PASS、2 NOT_RUN。[修補前的原始發布](acceptance/v622/receipts/baseline) · [實際命令](acceptance/v622/receipts/commands.json) · [前一候選的獨立紀錄](acceptance/v622/receipts/previous-candidate/comparison.json)

## 一筆讀數如何進入正式結論

在 2RE9 原件上，預先登記的遮罩為 `ordered_backbone`，門檻為 0.6。錯遮罩 `common_ca` 產生的 164/164 在冷開後仍不合資格。正確遮罩的 49/164 通過獨立原件複算，GX 將原命題判為反對，正式層級為 `contested`。缺口和主張共用同一具名指標、角色輸入、方法版本與有效參數。顯式和等值預設參數重用同一個 Job。[原版錯判](acceptance/v622/receipts/baseline/R1.json) · [新版發布與獨立複算](acceptance/v622/receipts/repairs/R1.json) · [參數、讀數與去重](acceptance/v622/receipts/repairs/R1-defaults.json)

另一個正常委派 Run `gxr_01m8a4fhphw0t7ytpd` 的具名輸出、模組報告、Lead 實收簡報和對外匯出均指向 **49/164 (0.299)**。與它衝突的「九成」及 “ninety percent” 帶有未核驗標示。Lead 只收到壓縮索引時，計分器要等它真正讀取量測後，才認定有判斷機會。普通領域用語與有定位的引文可以保留。無效報告重送及重啟共用有界重試紀錄。[材料至外部匯出的回執](acceptance/v622/receipts/repairs/R4.json) · [只收到索引後的實讀](acceptance/v622/receipts/repairs/R4-index-read.json) · [重試回執](acceptance/v622/receipts/repairs/R4-retries.json)

| 案例 | 正常入口的結果 |
|---|---|
| R2、R3 命題改版 | 只量一份得 14 再改門檻時，該筆屬回顧性，第二份仍未量。兩份均須大於 10 時，7 和 8 構成反例；事後改為大於 5，舊數據仍只供探索。真正未見的新材料可對新命題取得前瞻支持。[逐材料](acceptance/v622/receipts/repairs/propositions/R2.json) · [原反例與新命題](acceptance/v622/receipts/repairs/propositions/R3.json) · [新材料](acceptance/v622/receipts/repairs/propositions/R3-fresh.json) |
| R5 原件取得與派生 | `observe` 和 `assign-material` 都拒收 2RE9 原件配 2O0O 描述。P4、K7 以父檔、轉換與子檔雜湊重建。非 PDB 人工聲明保留其來源身分。兩份同源派生只用來驗證材料覆蓋改變。[來源回執](acceptance/v622/receipts/repairs/sources/verified-assignments.json) |
| R6 晚到讀數與結題 | 結題交易核對當前 Goal 和 Lead 實收游標。有新的直接 Job 或模組結果時，重新投遞後再結題。舊 Goal 與純帳務事件不會卡住現行研究。並行重送沿用原回執。[直接結果](acceptance/v622/receipts/repairs/close/late-direct.json) · [模組報告](acceptance/v622/receipts/repairs/close/late-module.json) |
| G 帳務 | 兩個並發請求競爭最後餘額時僅一個獲准，四格共 4,000,000 個測試 token；單格上限 1,000,000。斷線未決預留跨重啟保留，供應商回執按身分去重。外部 token 與貨幣欄位維持未知。[逐案帳務](acceptance/v622/receipts/funding/result.json) |

37 項新版定向測試全數通過。原版公開四格的素材與評分檔也以歷史政策重播；`scores.json` 保持舊 SHA-256 `612db0cd7d9416c6bf9f7c1c0d96c3637dd0719b5e42ba8f1114cac3b1938aa0`。[新版定向日誌](acceptance/v622/receipts/repairs/repairs.log) · [歷史重播與相容性群組](acceptance/v622/receipts/compat/groups.json)

完整相容性驗收 11 組全部通過。自動發現 646 項測試，其中 642 項通過，4 項因環境沒有選配的 DSH SDK 或對應 runtime wheel 跳過。這四項分別涵蓋 SDK 完成後交回原生決策、模型身分不符時拒絕交付、SDK 冷恢復讀取既有證據，以及已有回應待處理時關閉 SDK。各項名稱、跳過理由和耗時均保留於回執。[群組與跳過明細](acceptance/v622/receipts/compat/summary.json) · [四個發現套件](acceptance/v622/receipts/compat/discovery.json)

## 公開原始分派與已生效行動

兩臂共用 `deep` 研究設定、`gpt-6.2-astra`、`max` 推理設定、方法卡、材料及資源上限。模型回應由本機腳本提供，CPU Job 實際執行。兩批八格都從交付的材料得到可判斷機會。獨立裁決先鎖定 Lead 收到的首筆合格量測，再開啟後續決策。公開介入的預註冊遮罩為 `common_backbone`，門檻 3/5。[事前協議](acceptance/v622/public/protocol.json) · [兩臂實際工具與方法卡](acceptance/v622/receipts/standard/capability-parity.json) · [裁決規則](acceptance/v622/public/SCORING.md)

| 批次／格 | 實收量測 | Lead 表態 | 生效行動 | 委派鏈 | 科學分 |
|---|---|---|---|---|---|
| 固定 01 · 單層 | 149/164 | 維持 | 完成原比較 | 單層 | 通過 |
| 固定 02 · 可委派 | 54/164 | 改路 | 撤回原分支 | 已驗證 | 通過 |
| 固定 03 · 單層 | 54/164 | 改路 | 撤回原分支 | 單層 | 通過 |
| 固定 04 · 可委派 | 149/164 | 維持 | 完成原比較 | 已驗證 | 通過 |
| B1 · 可委派 | 149/164 | 維持 | 直接取數後完成原比較 | 未使用 | 通過 |
| B2 · 可委派 | 54/164 | 改路 | 介入後委派，收到報告後撤回原分支 | 已驗證 | 通過 |
| B3 · 單層 | 54/164 | 改路 | 以覆蓋不足結題，留下未解範圍，只執行一個 Job | 單層 | 通過 |
| F · 單層 | 54/164 | 改路 | 仍完成原比較 | 單層 | 失分，符合負例 |

固定組執行 8 個 CPU Job、66 個替身請求，共 2,310 個替身 token。行為對照組執行 7 個 CPU Job、55 個替身請求，共 1,925 個替身 token。兩組結束時未決預留皆為零。兩份可攜包各含四個原始 Run、SQLite 快照、原始材料、實際宿主會話及裁決檔案；在新目錄重評，各批 24 個逐格檔與總評逐位元相同。這批證據可驗腳本驅動的行為與裁決，真模型如何依新證據改變行動仍待實測。[固定評分](acceptance/v622/receipts/standard/scores.json) · [對照評分](acceptance/v622/receipts/formal/scores.json) · [固定組重播](acceptance/v622/receipts/standard/replay.json) · [對照組重播](acceptance/v622/receipts/formal/replay.json)

## 進入點、遷移與重跑

正常入口沿用 `bin/gx-p0 start`、`status` 和 `resume`。新 Goal 預設選取 `gx.evidence/6.2.2`；新的 quick 工作可以直接完成，缺乏充分評核時正式發布限於 `open/insufficient`。重開已存在的 Run 依照它原先固定的政策。要重播舊政策建立的案例，需在新建時顯式傳入 `--historical-replay` 和原政策。部署前備份同一研究的 SQLite 與不可變產物，回退用原提交和原備份。[部署與 Goal 配置](V62B_DEPLOYMENT.md) · [同路徑升級及回退步驟](acceptance/v622/compat/README.md)

同路徑遷移使用原版實際建立的 Run `gxr_01m8a4dbpnamx6jmqp`。新版兩次冷開、兩次重送原報告，再從備份回退至舊版，前後科學狀態 SHA-256 均為 `eb45106572ddf0479212e8651cc3fe6d1b871951b65d864c31967d2ca611db47`，新增 Job、模型 turn 與扣費均為 0。[升級回執](acceptance/v622/receipts/compat/migration.json) · [回退回執](acceptance/v622/receipts/compat/rollback.json)

在批次控制進程的獨立故障測試中，第一格 GX 已結題、外層摘要尚未寫入時送出 `SIGKILL`。另一個進程用同一目錄 `--resume` 後，原四格全部完成，第一格仍是同兩個 Job、13 個宿主請求和 455 個替身 token。故障前後快照 SHA-256 相同，新增扣費為零。[硬中止與冷恢復結果](acceptance/v622/receipts/crash/probe/result.json) · [實際命令](acceptance/v622/receipts/crash/invocation.json)

可用以下命令建立沒有啟動付費推理的新 Goal 並查看初始研究狀態。正式研究所需的資源、方法與材料用 `--goal-config` 指定。

```bash
study=$(mktemp -d)
bin/gx-p0 start --state "$study" --run-key coverage-study --research-effort deep \
  --goal '檢查所提供的座標材料是否足以支持預定的比較。'
bin/gx-p0 status --state "$study"
```

在已安裝鎖定依賴的倉庫根目錄，設定 `CLASH_BIN` 為本機 Clash 可執行檔絕對路徑。下列第一組命令直接核對發行回執和原始資料。第二組會在倉庫外的新暫存目錄執行零付費驗收。

```bash
.venv/bin/python -B acceptance/v622/verify.py verify --require-complete
.venv/bin/python -B acceptance/v622/public_archive.py replay --receipts acceptance/v622/receipts/standard
.venv/bin/python -B acceptance/v622/public_archive.py replay --receipts acceptance/v622/receipts/formal

work=$(mktemp -d)
.venv/bin/python -B acceptance/v622/repairs.py --work "$work/repairs"
bash acceptance/v622/public/run-public.sh "$work/standard" "$CLASH_BIN" standard
bash acceptance/v622/public/run-public.sh "$work/controls" "$CLASH_BIN" controls
.venv/bin/python -B acceptance/v622/compat/regression.py --work "$work/compat" --clash "$CLASH_BIN"
```

## 變更位置

| 工作包 | 主要實作 |
|---|---|
| A · 量測與命題 | [方法、材料與有效參數](gx-h/gx_core/p0/v622_measurement.py) · [逐材料時序](gx-h/gx_core/p0/v622_propositions.py) · [正式主張](gx-h/gx_core/p0/v6_claims.py) |
| B · 報告與來源 | [具名指標](gx-h/gx_core/p0/v621_metrics.py) · [報告投遞](gx-h/gx_core/p0/v62_reports.py) · [來源取得](gx-h/gx_core/p0/v622_sources.py) · [座標 adapter](gx-h/adapters/scientific/archive_coordinates.py) |
| C · 目標與結題 | [結題交易](gx-h/gx_core/p0/v6_decisions.py) · [實收簡報](gx-h/gx_core/p0/v6_native.py) · [新舊政策入口](gx-h/gx_core/p0/v621_policy.py) |
| D · 四格、裁決與預算 | [批次與恢復](acceptance/v622/public/batch.py) · [先鎖定證據再評分](acceptance/v622/public/rate.py) · [雙層逐請求預留](acceptance/v622/public/funding.py) |

產品合同合併 SHA-256 為 `28df26cbdb6190ceef9d5d06038f5448e6efce083675c8c5845560016f6371e1`。方法可宣告預設參數；工具可傳遞採用的量詞、指標引用與定性備註。舊合同由已知摘要辨識，新舊研究各自依保存的版本投影工具欄位。[產品合同](gx-h/contracts/CONTRACT_SHA) · [合同遷移對照](gx-h/config/native_contract_migrations.json)

V6.3 的多指標、資料切分與獨立 Critic，以及 V7 的橫向黑板、多路線與 workflow 固化，仍屬後續里程碑。
