# GX-H 科研 Harness 驗收

兩條指定原版分別是完整站 `4b6aa93` 與精簡站 `6ec604d`。正式研究取用的候選分別是 `362157ae` 與 `e9e41838`。首筆推理前於 2026-11-28 02:38:40 UTC 凍結兩站、插件規則、題料與評分設計。[雙站原始凍結單](candidate-freeze.json) · [操作、遷移與回退](OPERATIONS.md)

### 簽收索引

三個正式科研題均已啟動。A′ 與 B 由 Lead 自然結題，A 依預登記時限保存原 Run。以下各分母按實驗分列，同一 Run 在兩站交付時只計一次。

| 項目 | 版本或站點 | 實際分母與判斷 | 證據類別與原件 |
|---|---|---|---|
| PI、Join、問題身分、跨題通信等七項 | 完整與精簡兩站 | 逐項正例、反例、舊 Goal 相容及來源見下表；其中兩道原話各有一個獨立的官方模型首次開題 | [兩題正式 Run](pi/official-first-openings.json) · [兩站的部署與回歸](deploy/README.md) |
| 長作業與接管 | 三臂首試及正式 B | 三臂各一個在途 CPU Job，B 一個在途 GPU Job，均接回原本的提供者作業，額外實體啟動數各為 0 | LIVE_CODEX_CPU、LIVE_CODEX_GPU、FAULT_INJECTION。[CPU 身分](benchmark/long/field/output-verification.json) · [GPU 身分](gpu/first-run/README.md) |
| Recipe | 多階段受控、同域換料受控 | 兩個新題走出不同分支；另有一個歷史酵母配方換料後完成兩個新 CPU Job。正式 A→A′ 自主復用未觀察到 | SCRIPTED_CALLBACK、真 CPU。[多階段](macro/PORTABLE_STAGES.md) · [酵母配方](macro/same_domain/README.md) |
| A 食用酵母 | 首試完整站 | OPEN。23 回合、12 個 Job，11 成功。最後生效決定可證 4/5 項，正式終稿 0/1 | LIVE_CODEX_CPU/DB。[原場截止與證據](field/a/FINDINGS_AT_CUTOFF.md) |
| A′ 新酵母材料 | 首試完整站，獨立工作區 | CLOSED。12 回合、2 個成功 Job、兩份研究員報告。四項適用判據中三項完整、一項部分，第五項條件未觸發。跨背景的酵素活性占比維持未知 | LIVE_CODEX_CPU。[正式判讀](field/aprime/DECISION.md) · [冷開重導](field/aprime/cold-reexport.json) |
| B 天然乳蛋白 | 首試完整站，H800 | CLOSED。9 回合、2 個成功 Job。近全鏈比較支持負面結論，原機器裁決可判定 0/2 | LIVE_CODEX_GPU、真 CPU。[決定](gpu/first-run/DECISION.md) · [角色、映射與覆蓋](gpu/role-scope/README.md) |
| 長題三臂與短題 | 原生、完整基線、首試與另登記修後候選 | 長題依序為 7/7、無終稿、4/7，另登記的修後場為 7/7。短題兩次皆為 7/7，首次關題均遭拒 | LIVE_CODEX_CPU、匿名評閱。[原始計分與用時](benchmark/RESULTS.md) · [送評原包](benchmark/BLIND_PACKETS.md) |

### 七項改造

| 項目 | 起點與本次可複核結果 | 證據類別與回執 |
|---|---|---|
| 1．PI 意圖與能力綁定 | 兩個乾淨基線都把分階段題縮到 9 個方法而拒綁，卻讓全程限文獻的題目以 23 個方法通行。修後相應為 23 個並准綁、9 個並拒綁。子席只能在 PI 與自身委派的交集內選擇。 | SCRIPTED_CALLBACK、LIVE_CODEX。兩句原話各啟動一次正式 Lead，回調及同 Run 的實際綁定均符合條款。[雙站紅燈](pi/README.md) · [兩次官方首查與綁定結果](pi/official-first-openings.json) |
| 2．配方採納與 Join | 上游啟動後取消，下游仍未出生。兩個基線連續展開都留下 0 個 Join。修後強制終止並恢復，在 Run `gxr_01m8fzy44zk1ngnryh` 的事件 113，下一個 Lead 收到唯一有效的缺件通知。相同 prefix 與材料可在不同分支獨立採納。 | FAULT_INJECTION、SCRIPTED_CALLBACK。[中斷與交付](macro/ADOPTION.md) |
| 3．核心問題身分 | 省略、舊別名及根問題原文均命中同一份報告。同名子題只列有權可見的 ID。兩個已準備的結題請求只產生一次提示，後續讀到提示的 Lead 可給理由結題。 | SCRIPTED_CALLBACK。[六個受控回調情境，應提醒的 5/5 命中，零誤提醒](close/question-receipts.json) |
| 4．跨主題來源鏈 | 在 Run `gxr_01m8fzdy5e5p8yrtf8`，C 讀兩題並發表雙父綜合。完整授權的收件席可以追到雙根；缺少其中一份授權的收件席沒有摘要、ID 或喚醒。冷恢復後，上游更正可撤回下游先前看過的綜合。 | SCRIPTED_CALLBACK。[雙根、可見性與撤回](topics/synthesis-receipt.json) |
| 5．重讀舊原件與並發決策 | 原先可見但未採用的材料可支持同一意圖的新解釋，保留早先結論與其他路線。同輪 child 更新自身分支時，Lead 的原稿、預測門檻及兩種結果對應的行動維持原樣。新增 1 token 的旁支預留可與 CPU 計畫並行，該計畫的 CPU 資金、方法授權或已送達證據真正改變會觸發重新判斷。 | SCRIPTED_CALLBACK、FAULT_INJECTION。[重讀回執](topics/reopen-receipt.json) · [並發正反例](concurrency/test_scientific_premises.py) |
| 6．目錄與長作業 | 公開集前五召回從 3/8 個具名方法升至 8/8，四個正例從 1/4 題升至 4/4 題。另有四對事前封存的中英文問法，兩版基線中文 1/4、英文 4/4，候選兩語各 4/4 題至少命中一個合適方法。封存控制題僅參考與零命中各 1/1。兩個相容席位取得同一實體 Job、同一產物、一次結算。不同種子、私域、獨立重算另外核對。真 Driver 在 CPU 接單與回執之間死亡，新程序接回已有 Job。 | SCRIPTED_CALLBACK、FAULT_INJECTION、真 CPU。[公開目錄與方法分母](catalog/README.md) · [獨立目錄盲測](holdout/README.md) · [四庫實查](catalog/archive-job.json) · [Job 訂閱](jobs/shared-receipt.json) · [程序死亡](jobs/process-crash-receipt.json) |
| 7．交付與關題的證據 | 子席原件核驗須能追到實際讀取與已送達的報告；部分交付保留其具名產物。GX 可核對「已預測」的具名 Job。對原 PM₂.₅ 題回放，160 篇候選、41 篇 OA、9 篇定位閱讀，產生 0 次催讀和 0 個新 Job。 | SCRIPTED_CALLBACK、真 CPU、歷史 Run 唯讀重放。[交付與聲稱正反例](close/README.md) · [歷史案例](close/pm25-replay.json) |

### 多階段 Recipe、恢復與證據來源

保存的配方在新材料上建立新 Job，凍結條件由新題具名輸出重綁。60 分鐘與 45 分鐘的驗收資料走出不同分流。前一題使用 2 個 CPU Job，第一個決策後殺掉 Driver，冷開後回到同一決策，最後另做獨立評估；後一題使用 1 個 Job，停止評估。這個分母為兩個受控案例。自然選用配方的分母另見科研場試。[具名資料邊、決策和作業](macro/PORTABLE_STAGES.md)

另用歷史上已保存的酵母配方 `accession-pmid-coverage@1` 換入第三批公開材料。新 Run `gxr_01m8g2aj9bq8m0mpth` 實際完成兩個新 CPU Job，四個登錄號中有一個具備該條催化註解的文獻指標。受控 Lead 在事件 224 依本題的 1/4 與三個待查登錄號作決定，228 封存。這條回執證明引擎能復用配方；A、A′ 的自主行為另按其現場事件計分。[新舊輸入、兩項作業與決定](macro/same_domain/README.md)

同一筆上游資料透過 REST 與 MCP 各出一份原件，Lead 保留兩條引用與同一資料來源，主張為 `tested`；另一筆獨立採樣即便數值相同，主張可成為 `replicated`。這一組走普通 Goal，以受控來源回呼驗證歸因。[原始來源及主張分級](evidence/provider-sources.json)

### 相容性與短題首試

完整站與精簡站在掛載實際資料的環境，各自通過 121/121 項歷史回歸、92/92 項前版 Harness 回歸與 55/55 項本輪工程驗收。無掛載檢出各有 90/92 項前版回歸通過，另外 2 項為需要授權快照的跳過；新案例仍為 55/55。第一個整合版曾有一項舊題搜尋排名退步，修正後舊 Goal 的六題前十五項與原版逐欄一致。這套分母屬於 `fd1aa75`／`8759016`。[逐站原始分母與日誌](deploy/receipts/tests.json) · [舊題排序比對](catalog/cold-catalog-compatibility.json)

同一個近距離科學短題兩次盲評都是 **7/7**。本輪首試 `gxr_01m8g0b9dfdm3empcn` 用 377.28 秒、5 個官方回合、3 次結題提交、全程 7 筆決策拒收；上輪為約 501 秒、6 個回合、11 次結題提交、14 筆拒收。兩次首次結題提交均為 0/1。這些數字只描述這一道題。[首試官方回合、規格分類與盲評](benchmark/short/field/results.json)

### 長題三臂首試

三臂在相同 USGS 題料與方法下，各有一個 CPU Job 由提供端接單，在原計算程序存活時中斷控制器。重啟後三臂都消費了同一物理作業的輸出，同一請求的額外實體啟動均為 0。數值輸出與獨立 Decimal 參照相符。[三份物理輸出核對](benchmark/long/field/output-verification.json)

| 臂 | 科學盲評 | 時間與結束狀態 | 關鍵研究行為 |
|---|---:|---|---|
| 原生 Codex | 7/7 | 99.865 秒，完成 | 正確報總量比約 424.45，面積正規化後約 1.814。 |
| GX `4b6aa93` | 無終稿可評 | 900.04 秒，OPEN | 再次向 PI 詢問已寄出的面積表，停在原定時間上限。 |
| GX `362157ae` | 4/7 | 290.908 秒，CLOSED | 正確報總量比，卻稱面積仍待提供。Main 收到原表，關題的 Lead 沒有收到那兩條原文。 |

這是一道題，每臂只跑一次。修正晚到資料的轉交後另登記一次獨立回驗。兩個通知窗口、可見受眾及已處置資料都經普通 CLI 測試。[三臂及短題總表](benchmark/RESULTS.md) · [三臂逐項裁決](benchmark/long/field/results.json) · [原場回放與修補](late_pi/README.md) · [修後凍結單](followup-freeze.json)

### B · 天然乳蛋白的新預測與獨立評估

官方 Run `gxr_01m8g0ad2w0vh088y0` 已結題，9 個已確認原生回合與 2 個成功 Job。H800 的推理 Job `gxj_01m8g0d3d2amp59bj4` 產出新模型座標。只有在事件 77 發布座標後，保管席才於 94、98 提供實驗參照與位置對照。控制器在 GPU 程序確實持有裝置時被強制終止，新 Driver 取得原 workspace 鎖，接回同一 PID 與啟動時間，額外預測啟動 0 次。[提供者接管與時間線](gpu/first-run/README.md)

CPU 評分 Job `gxj_01m8g0v91xknqrv75g` 得到 **122/123 個可比 Cα 位置、RMSD 12.287726 Å**。獨立以另一格式的原座標及 QCP 配準重算，與正式 SVD 差約 1.78×10⁻¹⁵ Å。此結果不支持在預定 2.0 Å 門檻內吻合。Lead 接收讀數後於 160 採納負面主張，165 作出決定，193 提交結題，195 封存。冷開後原報告摘要相同，新增 0 Job、0 模型回合。[獨立復算](gpu/first-run/independent.json) · [Lead 正式決定](gpu/first-run/DECISION.md)

原 Run 的兩個自動命題均是 `Undetermined`，正式機器裁決為 **0/2**。參照、位置對照與模型共同參與評分，部分適用條件只寫在自然語言，無法充分歸到每項預先註冊的機讀判據。本次證據只涵蓋這條鏈及這個預先指定的沉積比較。把相同資料只取三個對照位置，RMSD 可降至 0.069947 Å，覆蓋卻僅 3/123。這個反例說明為何正式裁決必須同時固定預測、參照、位置映射與覆蓋分母。權重的訓練集是否曾見此沉積仍未知。[原始科學報告](gpu/first-run/REPORT.md) · [原版裁決的四組重放與覆蓋反例](gpu/role-scope/README.md)

### 修後真模型診斷

另行預註冊的候選是 `6077e26`／`ccd0513`，原三臂首試照原分數公布。長題 `gxr_01m8g13cqej2zc3fs1` 有正式 Lead 的原生送達回執，可以逐字比對兩站的面積資料。Lead 引用了新資料，決定總流量的十倍敘述可用、單位面積的十倍敘述不成立，事件 89 關題，獨立盲評 7/7。短題 `gxr_01m8g15z1x8rjmec38` 也是 7/7；253.43 秒，4 個回合、2 次結題、2 筆拒收，首次結題仍為 0/1。這次沒有自然出現原本的錯誤等待格式；拒收說明的效果只由原參數的定向重放證明。[修後長題](benchmark/long/followup/results.json) · [修後短題](benchmark/short/followup/results.json) · [原始格式拒收](watch_diagnostics/README.md)

### 書目修正後的雙站發行

完整站產品 `e8140c6c6efe5277bfb5aed4e70ab9ec175aae6b`，精簡站產品 `569657aed3faa72c12e0a7944c7e9dcfd849c3af`。兩站各有 54 個交付檔相對原版本改變；52 個改動後的 Git blob 相同，另兩個保留精簡站的專屬插件登記與 PDF 安裝提示。原場另發現的文獻卷號與年份誤拒，已用當時有權引用的 JATS 原件重放。書目可發表，實際數值仍需引證，未替先前的兩個完整報告補簽。模型給子席零次原生調用時，原回執會說明原委派無法開工及如何另開有預算的工作。[書目修正版的雙站部署樹](deploy/receipts/bibliography-installations.json) · [逐檔差異](deploy/receipts/bibliography-parity-including-adapters.json) · [原回呼重放](evidence/bibliography-replay.json) · [真場子席零回合](delegation/README.md)

各站實際掛載的發行結果為 **歷史 121/121、前版 Harness 92/92、本輪 67/67**。乾淨無掛載環境各有 **67/67** 項本輪測試通過，前版測試各 90 通過、2 個授權快照測試跳過。四組角色與歸檔資料摘要由各站實測。原近期 /2 Goal 從原部署冷開後，模型回合與 Job 的增量皆為 0。[逐站測試分母與日誌索引](deploy/receipts/bibliography-tests.json) · [部署首次失敗及復驗](deploy/README.md)

### A · 食用酵母的歸屬問題

`gxr_01m8g0a3qevpds66cg` 在首試 30 分鐘上限保存為 `OPEN`。23 個官方回合、12 個 Job（11 成功、1 逾時），到點時沒有在途 Job。Lead 已讀兩篇文章的相應表格，區分了 RNA 表達、純化製備的底物讀數與參考菌株的家族註釋，並採納分列、不合併的編目決定。庫查出了具名結果，但有效決策未完整解釋其覆蓋。截止前可核對 4/5 項科學判據，正式終稿 0/1。三位子席交回一份研究員簽署報告及兩份 GX 部分報告；沒有觀察到跨席線索使其他席改路，或 A 自行保存配方。[截止時的證據與恢復命令](field/a/FINDINGS_AT_CUTOFF.md) · [研究行為回執](field/a/behavior.json)


### A′ · 新材料上的酵母歸屬

`gxr_01m8g22y65r8zxbfqw` 自然結題。兩位子席分別交回一份 PARTIAL 和一份 COMPLETE，均有研究員署名。Lead 對照原始表、圖和兩份報告，將 2008 年的活性觀測限於指定菌株及抽提條件，保留 2024 年多背景研究支持的定性關聯。正式決定拒填三個菌株背景共同的酵素活性占比。決策 632 於事件 634 生效結題。冷重開封存件與科學記錄的摘要相同，新增 0 個 Job、0 個模型回合。[封存判據與逐條簽收](field/aprime/ASSESSMENT.md) · [當題判讀與原件引用](field/aprime/DECISION.md) · [冷重導](field/aprime/cold-reexport.json)

五條預登記判據中，兩篇來源身分、表型方向、拒絕跨株共用占比三項有完整證據。具名表格保留 0.18 與 0.79 等原始讀數，正式成品沒有明算二者的比值約 22.8%，數值判據列為部分達成。第五項只在決定查酵母庫時才適用，本題 Lead 選擇查原文。首場共有 12 筆決策拒收，留存了原生工具輸入、模型實際收到的回應及 GX 持久化回執。[逐次提交與結果](field/aprime/decision-friction.json)

### 現場修補與最終雙站版

完整站產品為 `1525adbce1d3b699b6b5594d686f18a36d7e9e8e`，精簡站產品為 `fdfa87f319649705d831d5602639134a3f43d90f`。A′ 在首試 605 的官方回應提到了缺引清單，實際沒有傳出 ID。資料庫另存了舊席無法執行的重試指引。首試 612 正確拒絕了原席已失效的租約。修補後，當 GX 能證明晚到的審閱全部出自現席、現席都已收到、且中間沒有 Job 改變前提時，會在正式工具回覆內交還缺少的具名 ID。現席可在原回合補引；若有新的外部資料，GX 仍實際交棒，舊席收到交接事件與終止回覆的指示。A′ 原件唯讀重放、兩個正式入口的受控時序、13 項相關既有回歸均已核驗。[兩站安裝樹與插件設定](release-installations.json) · [原場模型回執、交接與修後回歸](close_corrections/README.md)
