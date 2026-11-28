# 食用酵母資料表更正現場研究

A 追查兩筆 Ima1p 與麥芽糖的公開讀數，A′ 使用兩篇不同來源判斷 Nth2 的量化結論可否跨量測與菌株沿用。兩題均允許得到陰性或資料不足的結論。研究席只接收該題的問題、投稿人所指的記錄，以及第一篇原文。查哪些其他材料、是否開子研究席、是否保存或採用 Recipe，均由 Codex 決定。[事前封存](preregistered.json) · [原件、DOI 與摘要](materials.json) · [站點就緒紀錄](readiness.json)

## 部署與原件

由完整線 checkout 執行以下命令。設定 `PRIVATE` 為本試驗的原始持久目錄，`CANDIDATE` 為首筆推理前固定的完整產品提交。正式站的原始持久目錄為產品相鄰的 `../harness-final-field-private`。其中的 `archive/` 按材料清單中的名稱保存四篇原始公開 HTML／JATS 的逐字節副本。日後公開站點可能重新產生網頁，遇到摘要變動時使用保管者保存的已封存原件。

```bash
.venv/bin/python -B acceptance/harness_final/field/launch.py a "$PRIVATE" "$CANDIDATE"
.venv/bin/python -B acceptance/harness_final/field/launch.py aprime "$PRIVATE" "$CANDIDATE"
```

啟動器匯出已固定的產品樹、掛載現有四份唯讀資料庫，經 `bin/gx-p0 research` 使用官方 ChatGPT OAuth。A′ 經 `message --successor` 帶入自己的原件。重啟沿用同一個目錄與提交，狀態存在 `study/state/gx.db`、產物在同目錄的 `artifacts`、官方會話在 `study/codex/sessions`。帳戶憑證留在受權保管處。

## 唯讀複核

```bash
.venv/bin/python -B acceptance/harness_final/field/collect.py "$PRIVATE/study" "$RUN_ID" /tmp/food-field-receipt.json.gz
.venv/bin/python -B acceptance/harness_final/field/recheck_archive.py gx-h/var/science-assets/yeast-archive
```

匯出器對應 GX 的回合送達、官方 session、原文分段、工具回執、CPU Job 原產物、模組報告及 Lead 生效決定。科學得分依逐題封存的判準。是否觀察到同儕改路、研究員自發保存配方和新題實際採用，另依原始事件判斷。獨立 SQL 直接按分類、反應、物種及 accession 聯接來源表，保留每筆 assay 記錄和原始欄位，供核實局部快照的覆蓋。

2010 與 2008 兩篇在本次查詢 Europe PMC 的 fullTextXML 時得到 HTTP 500。兩份材料已從 PMC 正式文章頁取得，可核對封存 HTML 與對應表格位置。普通科研席仍可自己檢索另一篇來源。[取得紀錄](readiness.json)
