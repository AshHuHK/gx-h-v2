# 只給科學問題的公開文獻搜尋

`org.gx.publication-discovery@1.0.0` 已登記在普通新 Goal 的 `public_sources` 集合。研究員可只提供問題。要委派公開文獻子題時，deep Lead 可選用 `publication_discovery@1`。舊 Goal 仍能沿用它已固定的檢索器。

| 方法 | 已有輸入 | 科研參數 | 可引用的輸出 |
| --- | --- | --- | --- |
| `literature-query@1` | 空陣列 | `query` | `candidates.json` 和含官方原始回應的 `search-snapshot.json` |
| `literature-page@1` | 前一個 Job 的 `search-snapshot.json` | `action=next` 或 `replay` | 下一頁或重播結果。查詢詞、游標和來源取自快照 |
| `literature-original@1` | 已選頁的 `candidates.json` | 該頁列出的開放全文 `pmcid` | `article.xml` 和 `acquisition.json` |

## 一次無材料開案的回執

Run `gxr_01m8f6gwzh1dfvz1j7` 的題目是研究者如何判定都市空氣污染感測器的準確度。Goal 沒有研究材料、方法名或控制 JSON。Lead 和子研究員的工具回呼由測試控制，模型呼叫為 0。搜尋和取文的 CPU Job 經 Clash 連到 Europe PMC 和 Crossref 的正式服務。[行動與產物回執](evidence/publication-query.json)

| 動作 | GX Job | 核對結果 |
| --- | --- | --- |
| 首搜 | `gxj_01m8f6gxjd7jap1k01` | 准入輸入為 `[]`，每個來源各回傳 12 筆 |
| 續查 | `gxj_01m8f6hdh9a43eqhd3` | 兩個來源各取得第二頁 12 筆。每頁保留官方回應、游標、時間和內容摘要 |
| 重播 | `gxj_01m8f6hxhqzqgwdpnr` | 第二頁重播的快照 SHA-256 與上游逐位元一致，為 `49f85a49a7035c61cc2e6c9d23abc76506764d0041fdd70df87c207a1a2d81e8` |
| 原文 | `gxj_01m8f6hy1xb40njqrt` | 取得 PMC13629622 的完整 JATS。取得紀錄與 XML 的 SHA-256 均為 `676d1bee755798aa71c6d392ff2a154e9f34b297e8f0345e836f1256319eb565` |

錯把 `candidates.json` 交給續查方法時，GX 在派工前指出 `snapshot` 角色和出錯節點。冷開原 Run 的子研究員讀到文章正文 `body/sec1/p`，讀取回執為 `gxrc_01m8f6n87jqxhxf6k8`。Lead 收到其報告後採納 `gxa_54f915ef2a02a072d1e1445e1071b36f4c054bc6f5143fc938a869d3abe816ea`，保留原文供量測條件複核，同時記下此次抽查只覆蓋部分結果。[JATS 原件](https://www.ebi.ac.uk/europepmc/webservices/rest/PMC13629622/fullTextXML)

部署端的 `archived_originals` 目錄保存四份 GX Job manifest、具名輸出及原始 JATS。實際位置和所有內容摘要列在回執。期刊原文列示 CC BY-NC-ND 4.0 授權。[部署端產物索引](evidence/publication-query.json)

## 復驗

```sh
.venv/bin/python -B bin/gx-p0 plugin verify org.gx.publication-discovery@1.0.0
PYTHONPATH=gx-h:. .venv/bin/python -B -m unittest tests.test_publication_discovery
PYTHONPATH=gx-h:. .venv/bin/python -B acceptance/science_plugin_blueprint/publication_query.py "$new_work_directory" --export-originals "$archive_directory"
```

全新工作目錄會執行四個 Job。傳入已有完整 Job 回執的目錄，可以在同一 Run 讀回原文或匯出；這兩個步驟沿用已取得的產物。外部來源當下的篇目、開放狀態與覆蓋數會隨時間改變。原始回應快照可離線重播。
