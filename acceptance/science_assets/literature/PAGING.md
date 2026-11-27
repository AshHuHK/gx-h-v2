# 檢索頁窗和全文覆蓋

新頁窗套件為 `org.gx.scholarly-pages@1.1.0`，可供 `literature-query@2` 和其後繼方法調用。搜尋的候選、提供者原文位元、跨頁身份索引及讀取回執各有自己的範圍。

## 頁窗判定

原版讀到兩頁、每頁兩行且上游報四筆時，即使第二頁把同兩個 DOI 重排，也宣告穷盡。修後最新頁有兩行、新增零篇、累計兩篇。若游標仍可移動，可查下一窗；若相同身份重複且游標停滯或循環，記為續頁不明。局部重疊只增加實際新身份。已辨識足夠的唯一文獻才據上游總數判定看完。沒有總數的有效頁保留原件與讀數，故障保留為未取得。兩個提供者可以命中同一 DOI，各提供者的新增與合併後的新增可不同。[首次失敗](paging-before.log) · [定向回歸](paging-after.log)

`sources` 的 `returned` 為該頁上游行數，`indexed_records` 為有可辨識身份的行數，`provider_hits` 為上游所報總數，未知為 null。每個來源的 `new_unique` 與 `cumulative_unique` 從持久化的 DOI、PMID、PMCID 與具來源的 accession 歸併；`merged` 則再跨來源去重。舊 snapshot 若從第二頁開始，先前的行數仍可見，身份覆蓋標 `history_complete=false`，`new_unique=null`、累計為現存可識別文獻的下界。重播本身不新增計數。

`access` 的分母為已見唯一文獻。「開放」指上游明確標記，可以同時有摘要。摘要數指已封存的回應實際帶有摘要。搜尋操作本身取得的文章全文與定位閱讀均為零；後續獨立的取得 Job 和讀段回執才能提供這兩個數。資料庫總數和科學樣本獨立性不可互換。

## 普通 Goal 與實際公共來源

普通入場問題詢問城市空污感測器準確度評估。此次操作中，Lead 和 module 的動作由受控 native 回呼發出，0 個真模型回合。首查沒有附檔，四個 Job 皆為本 Run 真 CPU 子進程。原始問題、所選方法與各節點具名輸入輸出、摘要、讀取與 Lead 決策均在 [Run 回執](paging-field.json)。

| Run `gxr_01m8fdgmbaxg08wd5c` 的動作 | Job | 取得的證據 |
|---|---|---|
| 首窗 | `gxj_01m8fdgndddxhd0ek8` | Europe PMC 與 Crossref 各返 12 行，合併 24 篇 |
| 續窗 | `gxj_01m8fdgrqk6q080dq2` | 各有 12 行，各來源累計 24 篇，兩源合併累計 48 篇 |
| 離線重播續窗 | `gxj_01m8fdgzk1q8t9gpyn` | 兩份 snapshot 逐位元相同，來源與合併計數相同 |
| 取得正文 | `gxj_01m8fdh0nmgc78qjs8` | 已核對 PMC13629622 的 JATS 正文 SHA 與 accession；研究員閱讀其 p4 並提交報告，Lead 於事件 186 採納 |

當次續窗由資料源分別報 6,325 和 2,067,001 筆；兩者查詢語義不同，服務的總數會隨時間變動。48 篇中有 15 篇有開放標記、24 篇在搜尋位元內帶摘要。另經實際取得及讀段的文章為 1 篇。這個工程對照保留已採用的研究決策，Goal 處於 OPEN。另有前一輪實際兩窗以相同原始位元在新版離線重播，避免將它算成本輪新的網路查詢。[歷史窗重播](paging-archived.json)

從完整產品根目錄在原狀態繼續核驗既有回執或重新匯出。若要獨立重跑，請為新 Goal 指定全新的狀態路徑。研究者實際登入的模型另由真模型場驗收。

```bash
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B \
  -m acceptance.science_plugin_blueprint.publication_query \
  ../gx-science/asset-live/paging-v2 --method-version 2 \
  --export-originals ../gx-science/asset-live/paging-v2/originals
```

## 讀回正式部署的同一份原始資料

第一組四個網路和重播 Job 使用的頁窗套件摘要為 `33bce3499e79ffe1627a1dd36cf2bc42b324f1ad1dad05630e7ca341d9962b37`。額外反例發現某來源為同一個 accession 補登 DOI 時，累計統計已正確合併，單頁候選列表仍可能列兩篇。修後單頁與累計共用相同身份歸併，三行來源的實際排名均保留。最終套件摘要為 `6854b19b66618cd0e94014919f2ac0b55afc7a4534a7ca040f31982525e0bc66`。[同 accession 首次失敗](paging-accession-before.log) · [14 項回歸](paging-after.log)

正式版本從普通 Goal `gxr_01m8fds4fm6qgb7zz9` 開新案，綁定先前續窗的原始快照，CPU Job `gxj_01m8fdt412qekb9x4y` 確實重算。兩個來源的完整欄位與首次現場相同，跨來源仍為 48 篇。冷開同一 Run 後完成結題，合計一個 Job。候選與來源原件來自真實出版者，這次重新計算無網路，Lead 回呼受控。測試夾具首次草案漏寫分支條件，修後另一次草案早於讀數交付；兩者均在同 Run 留存，待已交付的原件進入新簡報後才正式結案。[終版 Job 與結題決策](paging-reviewed-goal.json) · [首次分支拒收](paging-replay-first.log) · [原件交付前拒收](paging-replay-early-context.log)

```bash
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B \
  -m acceptance.science_assets.literature.replay_goal \
  ../gx-science/asset-live/paging-v2-reviewed \
  ../gx-science/asset-live/paging-v2/originals/next/search-snapshot.json \
  ../gx-science/asset-live/paging-v2/receipt.json
```
