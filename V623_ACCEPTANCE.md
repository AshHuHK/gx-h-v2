# GX-H V6.2.3 部署與簽收

V6.2.3 將具名量測、同一資料庫內首次可得時間、實際送達的研究者，以及正式決策引用的數字連在一起。工程、公開四格和真原件分析使用不同的 Run。幾何研究的 PI 報告包含兩份沉積各自的結論、完整精度資料表與獨立複算。[工程驗收索引](acceptance/v623/evidence/index.json) · [科研報告](acceptance/v623/V623_SCIENCE_REPORT.md)

| 簽收範圍 | 所有者與結果 |
| --- | --- |
| V6.2.3 科研正確性 | 7°C 的既有讀數在改門檻或換措辭後仍按回顧性處理。具名指標可在原件入庫後首次算出；GX 核對實際 CPU 產物、actor 的送達回執和正式結論。衝突自由文字、大簡報、索引讀取、外部修訂和用途授權已做正反驗證。[工程回執](acceptance/v623/evidence/engineering/) |
| 生產入口 | 官方 App Server 的訂閱入口使用 ChatGPT OAuth 和 Clash。需要逐實體請求硬額度的另一系列已驗真宿主接線、計量和中斷恢復，預檢記錄其部署缺件。[訂閱入口](acceptance/v623/oauth/README.md) · [硬額度系列預檢](acceptance/v623/evidence/live/hard-preflight.json) |
| 公開四格 | 原硬額度系列真模型 0/4，工程演練 4/4，未見配對保留歷史 0/2。本次另行授權的 OAuth 四格已登記 4/4，啟動 1/4，獨立盲評啟動 0/4。第一格因時限及最後一筆未結用量停在原 Run。[OAuth 四格回執](acceptance/v623/oauth/receipts/first-attempts/index.json) · [原版演練回執](acceptance/v623/evidence/live/) |
| 真資料方法 | 2RE9、2O0O 各在兩種遮罩下完成具名 CPU Job。獨立程式由原始 mmCIF 重算，四份輸出全數符合容差，封存可重播。[四份獨立複算](acceptance/v623/evidence/independent/result.json) |
| 真科研答案 | 2RE9 的中位數會漏掉兩個 AC 鏈對例外。2O0O 的旋角在已測範圍內穩定，移除 21–40 會使三對鏈的 RMSD 變化均跨過 0.15 Å。該原件研究採公開回顧性設計，七次 Lead 決策來自官方宿主上的本機腳本。公開四格另測座標覆蓋下的真模型行動。[數值、判據與限制](acceptance/v623/V623_SCIENCE_REPORT.md) |

## 公開 OAuth 試點的實際結果

使用者授權研究與獨立盲評使用同一既有訂閱。官方帳戶在第一格啟動前及停止後均允許普通訂閱用量，帳戶相同。研究角色為 `gpt-6.2-astra/max`，研究政策為 deep。部署使用 0.169.0 官方 Linux 二進位檔與 Clash。註冊、對帳及匯出保存的全是同一批的原始分派。[入場回執](acceptance/v623/oauth/receipts/admission.json)

| 格 | 原始分派 | 原始結果 | 獨立盲評 |
| --- | --- | --- | --- |
| 01 | P4，單層 | Run `gxr_01m8aca38k35b8m45w`。GX 已接受 Lead 的 2 筆決策，並完成 1 個 CPU Job，在事件 72 實讀主鏈覆蓋 **149/164**；與獨立計數相符。首份計畫採五分之三作門檻。22:20:41 UTC，Lead 引用該讀數，提交沿用原方法的決策。請求漏填 `disposition`，GX 卻提示需指定已有比較。Lead 再次核對比較 ID 相符；8 分鐘時限到達時，該行動仍未生效，研究結果為 TIMEOUT。 | 尚未啟動，無科學分數。 |
| 02 | K7，可委派 | 已登記，尚未啟動。 | 尚未啟動。 |
| 03 | K7，單層 | 已登記，尚未啟動。 | 尚未啟動。 |
| 04 | P4，可委派 | 已登記，尚未啟動。 | 尚未啟動。 |

官方回應、GX 帳本與未結回應的交叉核對已保存。後續提交 `aa93c37` 在用量回執中另列可核實的下界；20 項對帳、控制器、盲評、匯出及冷恢復測試通過。[恢復回歸](acceptance/v623/oauth/receipts/recovery-tests.log)

前三個 turn 各有 82,502、64,324、98,459 token 完成對帳，合計 **245,285**。第四個 turn 的 12 個具供應商 ID 的回應另有 **333,311**，可核實的第一格用量下界為 **578,596 token**。最後一個具名回應後仍產生推理輸出，官方中斷事件附帶的累計量重複前值。原生歷史與官方帳戶查詢都沒有提供這一段的最終用量，整格總量及美元成本留空。依 PI 指定的前格結清條件，第 01 格保持 HELD，餘格維持原登記。這次實跑驗到真模型取數，證據驅動的後續行動、盲評及真委派鏈仍待驗。[第一格的計畫、行動與具名讀數](acceptance/v623/oauth/receipts/first-attempts/01.json) · [官方回執冷查與原帳本核對](acceptance/v623/oauth/receipts/interrupted-turn.json)

操作員可對原工作目錄執行 `reconcile`。若日後取得可歸屬的最終用量，沿用相同工作目錄、原始授權與原始 Run 接回盲評；研究時限及既有結果繼續保留。四格執行與原件區段敏感性研究分開計算。原件研究的授權只涵蓋本機 CPU。[操作與恢復指令](acceptance/v623/oauth/README.md)

## 來源與恢復

基線 `e6520f5` 的舊結果仍可按原評分器重播。新方法及規格首先於 `efc49fb` 凍結，核心、硬額度演練與 CPU 本例於 `82ee888` 實跑。OAuth 公開試點於 `0094e35` 綁定授權、宿主、材料、評分規則及工作目錄。四格按固定順序開啟；每格可用的模組及原始時間窗載於登記協議。[來源及舊版重播](acceptance/v623/evidence/historical/) · [四格協議](acceptance/v623/oauth/protocol.json)

同一路徑的舊 Run 已實做升級、重啟、重送與回退，原生契約、工作及用量均有前後指紋。一般遷移可在停止接單後保存原狀態，從候選版恢復同一 Run；回退使用升級前原狀態與原版本。新 Run 的證據政策及新產物保留在其建立版本。跨資料庫的歷史讀數須由操作員引入實際取得回執，才能納入前瞻性判定。[原 Run 升級與回退](acceptance/v623/evidence/upgrade/) · [部署基線](V622_ACCEPTANCE.md)

凍結候選的一筆測試夾具用了錯的材料角色，原 55 項中出現 1 個錯誤。對同一執行時改正夾具後重跑成功，並在 `0094e35` 重跑正反例。原生宿主在冷恢復時重新附加預設工作環境的問題，在 `30cc203` 以原角色設定、讀回核對及真程序強制停止重驗；沒有重複 Job、請求或計量。舊快速升級夾具隨歷史 CLI 取回當時的 Goal 格式，於 `82bd542` 通過。既有探索測試共 646 項，642 通過、4 項可選 DSH SDK 測試跳過。[原始及修正驗收](acceptance/v623/evidence/index.json) · [四項跳過理由](acceptance/v623/evidence/compat/skipped.json)

在正式部署之後，另以六個 Job 重現 512-byte 簡報把同名、不同遮罩的結果排在註冊方法之前。提交 `b40ba6b` 同時比對具名方法的版本、摘要與 Job 參數，兩筆註冊端點在 470 bytes 的交付中列在最前，餘項可按索引讀取。後續測試 18/18 通過。第一格實際只讀一個相關端點，原始讀取及評分分母照實保留。[真交付回執](acceptance/v623/followups/brief-priority/delivery.json) · [回歸紀錄](acceptance/v623/followups/brief-priority/regression.log)

## 復驗

使用倉庫鎖定環境，在倉庫根目錄執行。下列操作讀取保存的原件與回執，推理呼叫為零。新機環境依既有部署文件安裝鎖定宿主與依賴。[部署步驟](V61_DEPLOYMENT.md)

```bash
.venv/bin/python -B acceptance/v623/validate.py verify --require-complete
.venv/bin/python -B acceptance/v623/study/archive.py replay --receipts acceptance/v623/evidence/study
.venv/bin/python -B acceptance/v623/oauth/pilot.py preflight
```

研究、計量、暫停及接管指令與操作範圍載於相應入口。真模型的額外貨幣授權為零；提供者未出具金額時，美元欄保留未知。[OAuth 操作與原格恢復](acceptance/v623/oauth/README.md) · [硬額度入口](acceptance/v623/LIVE.md) · [CPU 原 Run 重播](acceptance/v623/evidence/study/README.md)
