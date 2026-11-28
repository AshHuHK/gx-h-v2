# 食用酵母資料表更正現場研究

A 追查兩筆 Ima1p 與麥芽糖的公開讀數，A′ 使用兩篇不同來源判斷 Nth2 的量化結論可否跨量測與菌株沿用。兩題均允許得到陰性或資料不足的結論。研究席只接收該題的問題、投稿人所指的記錄，以及第一篇原文。查哪些其他材料、是否開子研究席、是否保存或採用 Recipe，均由 Codex 決定。[事前封存](preregistered.json) · [原件、DOI 與摘要](materials.json) · [站點就緒紀錄](readiness.json)

## 首次成績

| 研究 | 普通入口及具名產物 | 截止情況 |
|---|---|---|
| A | `gxr_01m8g0a3qevpds66cg`，23 個官方回合，12 個 Job | 30 分鐘到時仍為 OPEN。5 條中 4 條已在受理的中途決策具證，沒有正式終稿。[逐條截止證據](a/FINDINGS_AT_CUTOFF.md) |
| A′ | `gxr_01m8g22y65r8zxbfqw`，12 個官方回合，2 個 Job | 事件 634 自然封存。3 條完整、1 條部分、1 條依科研選擇不適用。[逐條結題複核](aprime/ASSESSMENT.md) |

兩題皆已啟動，1/2 在註冊窗口自然結題。未觀察到 A 自發保存和 A′ 自發採納同一配方。另有獨立控制在新的食用酵母資料執行既有配方，實測第三組資料的文獻指標覆蓋為 1/4。[同域受控 Run](../macro/same_domain/README.md) · [舊研究再驗與差異](historical-recipe-verification.json)

## 部署與原件

由完整線 checkout 執行以下命令。設定 `PRIVATE` 為本試驗的原始持久目錄，`CANDIDATE` 為首筆推理前固定的完整產品提交。正式站的原始持久目錄為產品相鄰的 `../harness-final-field-private`。其中的 `archive/` 按材料清單中的名稱保存四篇原始公開 HTML／JATS 的逐字節副本。日後公開站點可能重新產生網頁，遇到摘要變動時使用保管者保存的已封存原件。

```bash
.venv/bin/python -B acceptance/harness_final/field/launch.py a "$PRIVATE" "$CANDIDATE"
.venv/bin/python -B acceptance/harness_final/field/launch.py aprime "$PRIVATE" "$CANDIDATE" --independent-after-hold
```

啟動器匯出已固定的產品樹、掛載現有四份唯讀資料庫，經 `bin/gx-p0 research` 使用官方 ChatGPT OAuth。A′ 經 `message --successor` 帶入自己的原件。重啟沿用同一個目錄與提交，狀態存在 `study/state/gx.db`、產物在同目錄的 `artifacts`、官方會話在 `study/codex/sessions`。帳戶憑證留在受權保管處。

A 在原註冊時間窗屆滿時仍為 OPEN。A′ 以同一已釘部署及原封存的新材料進入 `study-independent-aprime`，使用 `--independent-after-hold`。第一次嘗試將 A′ 放入仍有 A 的專案狀態時，普通入口在建立 Goal 前回應「This workspace already has a different project Goal」。隨後以獨立狀態開題。首次入場錯誤未送模型。兩題均只有一個正式 Run。[A 已受理的判讀與恢復位置](a/FINDINGS_AT_CUTOFF.md)

## 唯讀複核

```bash
.venv/bin/python -B acceptance/harness_final/field/collect.py "$PRIVATE/study" gxr_01m8g0a3qevpds66cg /tmp/food-field-a-receipt.json.gz
.venv/bin/python -B acceptance/harness_final/field/collect.py "$PRIVATE/study-independent-aprime" gxr_01m8g22y65r8zxbfqw /tmp/food-field-aprime-receipt.json.gz
.venv/bin/python -B acceptance/harness_final/field/recheck_archive.py gx-h/var/science-assets/yeast-archive
```

匯出器對應 GX 的回合送達、官方 session、原文分段、工具回執、CPU Job 原產物、模組報告及 Lead 生效決定。科學得分依逐題封存的判準。是否觀察到同儕改路、研究員自發保存配方和新題實際採用，另依原始事件判斷。獨立 SQL 直接按分類、反應、物種及 accession 聯接來源表，保留每筆 assay 記錄和原始欄位，供核實局部快照的覆蓋。

2010 與 2008 兩篇在本次查詢 Europe PMC 的 fullTextXML 時得到 HTTP 500。兩份材料已從 PMC 正式文章頁取得，可核對封存 HTML 與對應表格位置。普通科研席仍可自己檢索另一篇來源。[取得紀錄](readiness.json)

事前封存的評分原文和快照參考在兩題原窗口結束後公開，可逐一對照 `preregistered.json` 的摘要。另存獨立 SQL 的來源行以及每條 assay 的原欄值。[判準](rubric.json) · [原快照參考](archive-oracle.json) · [獨立直接 SQL](independent-sql.json)
