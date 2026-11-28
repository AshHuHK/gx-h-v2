# 雙語目錄與酵母快照驗收

普通 `gx research` 建立 Goal。測試透過受控 native callback，以 Lead 身分呼叫 `gx_query_catalog`，每題在混合目錄取前五。正例分別計「至少命中一個相關方法」的題數及「進入前五的相關方法」的數量。可執行命中要求就緒、已安裝且已授權啟用或選用。查到一張方法卡本身不會啟用該方法。這批回執的證據類型是 `SCRIPTED_CALLBACK`。

| 公開集及掛載 | 至少一個相關方法 | 相關方法召回 | 六次查詢僅命中參考卡 | 六次查詢零命中 |
|---|---:|---:|---:|---:|
| 完整基線 4b6aa93 | 1/4 | 3/8 | 2 | 2 |
| 精簡基線 6ec604d | 1/4 | 3/8 | 2 | 2 |
| 雙語目錄候選 8a5fe74 | 4/4 | 8/8 | 1 | 1 |

兩版的主要缺口均可用環肽和乳糖的中英文查詢重現。每題實際前五、方法識別碼、授權狀態、就緒狀態及匹配原因保存在 `baseline-pinned-*.json` 與 `candidate-public.json`。首輪完整站另見 `site-full-first.json`，它被一份額外 Python bytecode 改變 provider 摘要所影響，後續同站觀察在 `site-full-still-unavailable.json`。已授權完整及精簡基線在獨立乾淨掛載檢出取得上表的結果。啟動外部 Python 檢查時可加 `-B` 避免污染 provider 安裝樹；正式 Job 的 provider 摘要在使用前後均經過核對。

獨立保管者在候選提交前封存另外八個雙語正例及兩個控制題。承諾摘要為 `d4be93a938559e281cc022b55d47c9eeae11a8117ec28e7a5f83514f322b6407`。首次 `8a5fe74` 得中文 4/4、英文 4/4、僅參考 1/1、零命中 1/1。最終雙線逐題回執由其獨立交付，公開開發集分母如上。

## 插件與方法盤點

`installed-methods.json` 記錄候選完整站的 50 筆公開方法卡，包括 provider、版本、摘要、名稱、別名、現行輸入和輸出、目的、適用範圍和限制。48 筆有插件提供者的方法都有繁中、簡中、英文名稱；另外兩筆是內建 artifact-analysis 和測試 fixture-grid。空的適用欄位代表原方法沒有宣告該細項，使用者仍可讀到已登記的限制。完整及精簡站各自維持原有 Goal 的啟用與註冊權限。

新提供者可在自身已驗證的 `gx_plugin.json` 的 `labels.gx_catalog_terms` 指定包內 JSON。檔案以 `capability@version` 為鍵，每筆含 `names`（語言到顯示名的映射）與 `aliases`（同義詞字串陣列）。既有插件的名稱由無可執行能力的 `org.gx.catalog-translations@1.0.0` 宣告，按原 provider、方法及 portable SHA-256 精確命中。提供者自己的文字優先。原 Goal、正式選用、Job 准入、參考卡執行權都沿用原有 owner。輸出缺失、未掛載和本題曾執行是各自獨立的事實。

## 四份快照與具名結果

`mounted-snapshots.json` 記錄現場四份資料逐位元摘要。乾淨環境 15 項針對性測試中有 13 通過，兩項需要本站授權資料的真 Job 明示跳過。掛載站 15/15。錯版或部份資料不齊的已部署掛載仍須通過完整性核對，不能進入這兩項跳過。原始兩項環境誤判留在 `first-clean-missing.log`。

| 讀取狀態 | 驗證方式 | 科學輸出 |
|---|---|---|
| 未掛載 | 普通 Lead 看見已安裝、依賴未就緒、未執行。正式 reader 缺少檔案 | 無 |
| 毀損 | 固定摘要不符且 SQLite 完整性檢查失敗 | 無 |
| 另一完整版本 | 固定摘要不符，SQLite 可讀且完整性檢查通過。只可判定未登記版本，不能從未知摘要命名發行版 | 無 |
| 四份摘要相符 | 真 CPU Job `gxj_01m8fzm413trpgcra8` 執行 S288C 乳糖的原始查詢 | `parent-records.json`，exact accession 為 0 |

最後一項普通 Run 為 `gxr_01m8fzm3wx576v82xj`。產物 SHA-256 為 `a275c0a8e16f024256c7eda279340104967431fd1e9713bcd78f9b6718a534aa`。結果只支持此次資料庫快照未找到精確 accession，活性仍未知。參見 `archive-job.json` 與同目錄的產物原始位元組。授權快照沒有複製到發行庫。

在發行根目錄可重跑下列命令。

```sh
export GX_CODEX_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a
python -B acceptance/harness_final/catalog/evaluate.py --repo . --corpus acceptance/harness_final/catalog/public.json --output /tmp/gx-public-search.json
python -B acceptance/harness_final/catalog/inventory.py
python -B acceptance/harness_final/catalog/archive_snapshots.py
python -B -m unittest -v acceptance.harness_final.catalog.test_discovery acceptance.harness_final.catalog.test_archive acceptance.harness_refactor.field.test_literal_archive acceptance.harness_refactor.catalog.test_open_discovery
```

### 已保存 Goal 的版本相容性

正式候選 `362157a` 跑既有 121 項測試時，有一個無 harness 版本標記的早期 Goal 在「文獻／文献」前十五項找不到 `scholarly-fetch@1`。首次失敗見 `first-legacy-ranking.log`。原因包括新翻譯欄位及精確別名排序加權均套用到了舊 Goal。修補沿建立時保存的 Goal 版本投影這兩項變動，保留 /2 及無標記 Goal 的原始方法別名和排序。

`cold-catalog-compatibility.json` 的每個版本分別從其原始普通 Goal 開題。停止程序後，以同一 source 和 state 目錄冷開並查詢。4b6aa93 的 /2 與 362157a 的 /3 各驗六題；前十五項的識別碼、類型、provider、狀態與匹配原因全數相同。兩個 Goal 的查詢結果分別與各自版本在修補前的回執對照。原 121 項中失敗的案例及六個相鄰測例亦重驗通過。後續修補不影響已在跑的 /3 研究。
