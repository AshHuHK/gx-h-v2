# 零附件文獻研究與來源覆蓋

本題讓市府判斷沿海與內陸的低成本光學粒子感測器能否用來排列兩區的日平均 PM₂.₅。題面只要求有定量現場根據的建議，沒有提供研究材料、方法名、論文名或答案。原始 Goal 採 deep，允許在已部署的公開來源範圍內選法。Lead 決定查詢、選文及是否委派。[原題](question.md) · [入場設定](goal.json) · [預先固定的判讀口徑](preregistered.json)

## 首次真模型結果

普通 Goal `gxr_01m8fd9bzcpwt0qr1n` 在決定 1612 後，於事件 1613 正式結題。Lead 建議在濕度和覆蓋條件可比時，用各站相對自身基線的大幅變化及同步峰值，標示可能的區域污染事件。若要報告哪區較高，先把兩台儀器放在同一參照旁檢查偏移，再於兩個部署點用獨立日期和同步參照檢驗校正後日差；日差的不確定區間落在零的一側，才公布該日排序。擴大到街區之前還須評估站點與季節的代表性。[Lead 完整決定](DECISION.md) · [一頁研究報告](REPORT.md) · [原始結題匯出](result.json)

| 現場項目 | 實際結果 |
|---|---|
| 原生模型 | 同一官方 OAuth 經 Clash，23 回合，Lead 10、子研究員 13 |
| 執行 | 9 個查詢 Job、11 個原件取得 Job，19 成功、1 次上游逾時，GPU 0 |
| 文獻覆蓋 | 9 個視窗共 160 篇，其中 41 篇有開放標記。10 次成功取得對應 9 篇不同原文，9 篇均有具名讀段；其餘 151 篇的摘要覆蓋在舊版未知 |
| 實際閱讀 | 806 筆成功段落回執，對應 421 個不同原件位置。兩個子研究員分別從不同候選產物取得 PMC11900072，位元摘要相同，在 160 篇及 9 篇原件中各計一次 |
| 並行與交接 | 不同子研究員的原生回合最長重疊 194.003 秒，CPU Job 最長重疊 6.453 秒。三份由原生研究員署名的報告完成，一份由 GX 交付部分結果。1340 的具名子報告有 Lead 讀取原話並採納主張的完整回執 |
| Skill | 6 次短目錄查詢展示技能，具名完整技能卡開啟 0 次。已發生的查原文及交叉比對可直接核對。對特定技能的主觀採用、放棄原因與因果影響均無回執 |

25 分鐘時，首個 Driver 保存 21 個模型回合與全部 20 個 Job。以同一 Run ID 恢復後另有兩個回合，外部 Job 增量為零。事件 1593 記下舊簡報之後的新證據，1600 記下已被取代的 Root 再度送件遭拒。新的 Lead 持有更新後的簡報，正式決定已包含最後到場的審查。結案後另以離線宿主設定冷開，確認 Job 與模型回合增量皆為零，正式決定、結題事件與全文覆蓋均與封存輸出一致。[逐回合、Job、閱讀與交接](field.json) · [首個驅動視窗](first-drive.json) · [恢復及離線冷開](recovery.json)

## 研究操作與恢復

從完整產品根目錄使用既有 Codex OAuth 與本機 Clash。以下命令不需要在 shell 填入金鑰。原狀態位於產品目錄相鄰的 `../gx-science/asset-live/literature`，其正式 Run ID 為 `gxr_01m8fd9bzcpwt0qr1n`。若在乾淨部署開另一個工程驗證，使用新的研究目錄；選擇先前的 Run ID 可續行同一題。

```sh
study=../gx-science/asset-live/literature
case_dir=acceptance/science_assets/literature
export GX_P0_CODEX="$PWD/vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$study" research \
  --state "$study/state" --run-key city-field \
  --goal-file "$case_dir/question.md" --goal-config "$case_dir/goal.json"

.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$study" research \
  --state "$study/state" --run-id gxr_01m8fd9bzcpwt0qr1n

PYTHONPATH=gx-h:. .venv/bin/python -B "$case_dir/collect.py" "$study"
PYTHONPATH=gx-h:. .venv/bin/python -B "$case_dir/read_table.py" "$study/state/gx.db"
```

同一 Run 中，第一個成功查詢距 Goal 建立 59.886 秒，第一份核驗原文在 154.194 秒返回。小組交付 1340 的研究報告含可定位原件，Lead 的具名正文讀取、引用決定 1527 及其科學主張採納事件 1522 已能連到同一回執。中途一筆 South Florida 原文取得 Job `gxj_01m8fdvefnz9k91fqw` 在經 Clash 讀取上游時逾時。Lead 保留了這項缺口，改用已取得的其他現場原件檢驗比較是否成立。

本題與先前有附件的夜光研究可對讀。夜光研究的 Lead 從 35 張短卡中看到 `gx2understand@1` 排首位，實際選讀 `source-evidence-review@1` 正文，之後才讀取 JATS 指定段落和採納判斷。這段順序可重查；該次 Lead 對卡片影響的主觀說明仍缺錄。[先前真模型選卡與閱讀](../../science_plugin_blueprint/reading-field/README.md)

## 如何讀正式研究頁上的覆蓋數

普通研究結題匯出的 `result.json` 若有出版物搜尋，會多出 `publication_coverage`。每個視窗保留其查詢、搜尋來源、上游回報總量與實際回傳行數；新版讀取器也給出新增及累積的唯一出版識別和歷史是否完整。每個來源的 `scientific_access` 另行標示本 Run 中哪些已列的出版物有已匹配原件，以及哪些有真正的具名段落閱讀回執。`original_job_ids_from_this_output` 指出哪個具體候選產物曾作為取得原文的輸入。若一份原件同時在兩個來源或兩個查詢列出，合併總數仍只算一篇。

`verified_full_text` 來自完成的 Job 和已登記、匹配版本的原件；`located_full_text` 還要求原生工具實際返還 `gx_read_source` 段落。摘要旗標與完整閱讀分開計。舊候選沒有記錄摘要時，未取得原件的資料列入未知或僅書目；有 OA 旗標本身也無法證明已經下載。回放同一頁可改成一個新 Job，該視窗另標 `snapshot_replay`。首次活體查詢使用已部署的 @1 執行讀取器；修後 @2 的真實來源回應、精確原始位元回放與獨立普通 Goal 各有回執。[分頁修正與 CPU 實跑](PAGING.md) · [含原件及閱讀的 @2 視窗](controlled-window-coverage.json) · [已結題的 @2 正式匯出](controlled-report-export.json)

獨立 XML 解析器直接讀取同一篇原件的兩個位置。西費城研究於 2024 年夏季、2025 年春季以共址稱重濾膜對照日平均值。原文第 3.2 節報告，感測器原值 RMSE 為 6.00 µg/m³，修正後為 1.605 µg/m³，與 Lead 的核心讀數一致。另一站 Villanova 的 Table 3 使用不同輸出通道；該表全期、24 小時均值欄位的 RMSE 為 5.249 及 1.670 µg/m³，差 3.579 µg/m³。兩座未共址的市府站點之間的偏差仍須另外量測。[數值複讀與運算](table-check.json) · [原始研究](https://doi.org/10.1021/acsestair.5c00443)
