# GX-H 科研 Harness 驗收

兩條指定原版分別是完整站 `4b6aa93` 與精簡站 `6ec604d`。正式研究取用的候選分別是 `362157ae` 與 `e9e41838`。首筆推理前於 2026-11-28 02:38:40 UTC 凍結兩站、插件規則、題料與評分設計。[雙站原始凍結單](candidate-freeze.json) · [操作、遷移與回退](OPERATIONS.md)

### 七項改造

| 項目 | 起點與本次可複核結果 | 證據類別與回執 |
|---|---|---|
| 1．PI 意圖與能力綁定 | 兩個乾淨基線都把分階段題縮到 9 個方法而拒綁，卻讓全程限文獻的題目以 23 個方法通行。修後相應為 23 個並准綁、9 個並拒綁。子席只能在 PI 與自身委派的交集內選擇。 | SCRIPTED_CALLBACK、LIVE_CODEX。兩句原話各啟動一次正式 Lead，回調及同 Run 的實際綁定均符合條款。[雙站紅燈](pi/README.md) · [兩次官方首查與綁定結果](pi/official-first-openings.json) |
| 2．配方採納與 Join | 上游啟動後取消，下游仍未出生。兩個基線連續展開都留下 0 個 Join。修後強制終止並恢復，在 Run `gxr_01m8fzy44zk1ngnryh` 的事件 113，下一個 Lead 收到唯一有效的缺件通知。相同 prefix 與材料可在不同分支獨立採納。 | FAULT_INJECTION、SCRIPTED_CALLBACK。[中斷與交付](macro/ADOPTION.md) |
| 3．核心問題身分 | 省略、舊別名及根問題原文均命中同一份報告。同名子題只列有權可見的 ID。兩個已準備的結題請求只產生一次提示，後續讀到提示的 Lead 可給理由結題。 | SCRIPTED_CALLBACK。[六個原生情境](close/question-receipts.json) |
| 4．跨主題來源鏈 | 在 Run `gxr_01m8fzdy5e5p8yrtf8`，C 讀兩題並發表雙父綜合。完整授權的收件席可以追到雙根；缺少其中一份授權的收件席沒有摘要、ID 或喚醒。冷恢復後，上游更正可撤回下游先前看過的綜合。 | SCRIPTED_CALLBACK。[雙根、可見性與撤回](topics/synthesis-receipt.json) |
| 5．重讀舊原件與並發決策 | 原先可見但未採用的材料可支持同一意圖的新解釋，保留早先結論與其他路線。同輪 child 更新自身分支時，Lead 的原稿、預測門檻及兩種結果對應的行動維持原樣。新增 1 token 的旁支預留可與 CPU 計畫並行，該計畫的 CPU 資金、方法授權或已送達證據真正改變會觸發重新判斷。 | SCRIPTED_CALLBACK、FAULT_INJECTION。[重讀回執](topics/reopen-receipt.json) · [並發正反例](concurrency/test_scientific_premises.py) |
| 6．目錄與長作業 | 封存目錄四對中文／英文問法，兩版基線中文 1/4、英文 4/4；候選兩語各 4/4，相關方法均進入前五。僅參考和真零各 1/1。兩個相容席位取得同一實體 Job、同一產物、一次結算。不同種子、私域、獨立重算另外核對。真 Driver 在 CPU 接單與回執之間死亡，新程序接回已有 Job。 | SCRIPTED_CALLBACK、FAULT_INJECTION、真 CPU。[獨立目錄盲測](holdout/README.md) · [四庫實查](catalog/archive-job.json) · [Job 訂閱](jobs/shared-receipt.json) · [程序死亡](jobs/process-crash-receipt.json) |
| 7．交付與關題的證據 | 子席原件核驗須能追到實際讀取與已送達的報告；部分交付保留其具名產物。GX 可核對「已預測」的具名 Job。對原 PM₂.₅ 題回放，160 篇候選、41 篇 OA、9 篇定位閱讀，產生 0 次催讀和 0 個新 Job。 | SCRIPTED_CALLBACK、真 CPU、歷史 Run 唯讀重放。[交付與聲稱正反例](close/README.md) · [歷史案例](close/pm25-replay.json) |

### 多階段 Recipe、恢復與證據來源

保存的配方在新材料上建立新 Job，凍結條件由新題具名輸出重綁。60 分鐘與 45 分鐘的驗收資料走出不同分流。前一題使用 2 個 CPU Job，第一個決策後殺掉 Driver，冷開後回到同一決策，最後另做獨立評估；後一題使用 1 個 Job，停止評估。這個分母為兩個受控案例。自然選用配方的分母另見科研場試。[具名資料邊、決策和作業](macro/PORTABLE_STAGES.md)

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

這是一道題，每臂只跑一次。修正晚到資料的轉交後另設獨立單例。兩個通知窗口、可見受眾及已處置資料都經普通 CLI 測試。[三臂逐項裁決](benchmark/long/field/results.json) · [原場回放與修補](late_pi/README.md) · [修後凍結單](followup-freeze.json)

### B · 天然乳蛋白的新預測與獨立評估

官方 Run `gxr_01m8g0ad2w0vh088y0` 已結題，9 個已確認原生回合與 2 個成功 Job。H800 的推理 Job `gxj_01m8g0d3d2amp59bj4` 產出新模型座標。只有在事件 77 發布座標後，保管席才於 94、98 提供實驗參照與位置對照。控制器在 GPU 程序確實持有裝置時被強制終止，新 Driver 取得原 workspace 鎖，接回同一 PID 與啟動時間，額外預測啟動 0 次。[提供者接管與時間線](gpu/first-run/README.md)

CPU 評分 Job `gxj_01m8g0v91xknqrv75g` 得到 **122/123 個可比 Cα 位置、RMSD 12.287726 Å**。獨立以另一格式的原座標及 QCP 配準重算，與正式 SVD 差約 1.78×10⁻¹⁵ Å。此結果不支持在預定 2.0 Å 門檻內吻合。Lead 接收讀數後於 160 採納負面主張，165 作出決定，193 提交結題，195 封存。冷開後原報告摘要相同，新增 0 Job、0 模型回合。[獨立復算](gpu/first-run/independent.json) · [Lead 正式決定](gpu/first-run/DECISION.md)

原 Run 的兩個自動命題均是 `Undetermined`，正式機器裁決為 **0/2**。參照、位置對照與模型共同參與評分，部分適用條件只寫在自然語言，無法充分歸到每項預先註冊的機讀判據。本次證據只涵蓋這條鏈及這個預先指定的沉積比較。權重的訓練集是否曾見此沉積仍未知。[原始科學報告與適用範圍](gpu/first-run/REPORT.md)

### 修後真模型診斷

另行預註冊的候選是 `6077e26`／`ccd0513`，原三臂首試照原分數公布。長題 `gxr_01m8g13cqej2zc3fs1` 有正式 Lead 的原生送達回執，可以逐字比對兩站的面積資料。Lead 引用了新資料，決定總流量的十倍敘述可用、單位面積的十倍敘述不成立，事件 89 關題，獨立盲評 7/7。短題 `gxr_01m8g15z1x8rjmec38` 也是 7/7；253.43 秒，4 個回合、2 次結題、2 筆拒收，首次結題仍為 0/1。這次沒有自然出現原本的錯誤等待格式；拒收說明的效果只由原參數的定向重放證明。[修後長題](benchmark/long/followup/results.json) · [修後短題](benchmark/short/followup/results.json) · [原始格式拒收](watch_diagnostics/README.md)

### 最終工程發行

完整站產品 `e8140c6c6efe5277bfb5aed4e70ab9ec175aae6b`，精簡站產品 `569657aed3faa72c12e0a7944c7e9dcfd849c3af`。兩站各有 54 個交付檔相對原版本改變；52 個改動後的 Git blob 相同，另兩個保留精簡站的專屬插件登記與 PDF 安裝提示。原場另發現的文獻卷號與年份誤拒，已用當時有權引用的 JATS 原件重放。書目可發表，實際數值仍需引證，未替先前的兩個完整報告補簽。模型給子席零次原生調用時，原回執會說明原委派無法開工及如何另開有預算的工作。[雙站完整部署樹](release-installations.json) · [逐檔差異](deploy/receipts/bibliography-parity-including-adapters.json) · [原回呼重放](evidence/bibliography-replay.json) · [真場子席零回合](delegation/README.md)

各站實際掛載的發行結果為 **歷史 121/121、前版 Harness 92/92、本輪 67/67**。乾淨無掛載環境各有 **67/67** 項本輪測試通過，前版測試各 90 通過、2 個授權快照測試跳過。四組角色與歸檔資料摘要由各站實測。原近期 /2 Goal 從原部署冷開後，模型回合與 Job 的增量皆為 0。[逐站測試分母與日誌索引](deploy/receipts/bibliography-tests.json)

### A · 食用酵母的歸屬問題

`gxr_01m8g0a3qevpds66cg` 在首試 30 分鐘上限保存為 `OPEN`。23 個官方回合、12 個 Job（11 成功、1 逾時），到點時沒有在途 Job。Lead 已讀兩篇文章的相應表格，區分了 RNA 表達、純化製備的底物讀數與參考菌株的家族註釋，並採納分列、不合併的編目決定。庫查出了具名結果，但有效決策未完整解釋其覆蓋。截止前可核對 4/5 項科學判據，正式終稿 0/1。三位子席交回一份研究員簽署報告及兩份 GX 部分報告；沒有觀察到跨席線索使其他席改路，或 A 自行保存配方。[截止時的證據與恢復命令](field/a/FINDINGS_AT_CUTOFF.md) · [研究行為回執](field/a/behavior.json)
