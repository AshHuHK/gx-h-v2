# 從書目缺口派生方法，換題後再計算

兩個研究都問已知搜尋窗中多少筆記錄提供 PMC 原件定位碼。第一題使用城市空污感測器的搜尋回應；第二題使用另一個 Run 對沿岸、霧與感測器比較的不同搜尋回應。事前的讀法以「來源代碼加記錄 ID」辨認每篇記錄，一筆有多個指標時只計一次。空定位碼留在分母。只會影響原件取得排序，無法用來判定文獻品質或全文是否存在。[讀法原件](method-source.md) · [輸入、原 Job 及 SHA](provenance.json)

| 階段 | 本次結果 |
|---|---|
| 父題開發 | `gxr_01m8fdsz3xhgnrzphv` 經普通 `research` 入口啟動。CPU Job `gxj_01m8fdszm9h9912nq7` 從讀法原件產生具引文定位的規格，測得校準集 2/4，保留重複 ID、空值、未配到、集合外及空分母反例。普通 `gx_install_record_method` 發布實際可安裝的 `record-coverage-6c93f523499b66fbf42e7ff4@1`。用一般 cohort 檔冒充開發作業輸出曾遭拒絕，重送合法安裝得到相同回執。 |
| 父題計算 | 新 CPU Job `gxj_01m8fdt0qc9epsryp1` 測得 **7/12**。Lead 實讀本題的具名 `coverage.json`，結題決策將七筆有定位碼的記錄列為後續取得原件的對象。 |
| 新題選法及計算 | 新 Run `gxr_01m8fdt1kq6t494ff4` 由普通後繼研究訊息建立。其初始範圍容許選取父題方法。Lead 以輸出 `coverage.json` 搜尋、精讀方法卡並啟用；實際 Job `gxj_01m8fdt23rczyb0bxy` 使用自己的兩份新輸入，得 **6/8**，引用自己的讀數結題。 |
| 獨立復核 | SQLite 直接聚合供應商原始回應的 source、id 與 pmcid，分子與分母兩題均吻合。每份輸出也逐一核對了所用 cohort 與 evidence 的 SHA。這兩題沒有下載或閱讀原文。 |

以上 2 個已結題 Goal 使用受控原生回呼，GX 執行 3 個外部 CPU Job，模型回合為 0。可簽收方法資產被實際建立、安裝、在兩個問題使用。後題查詢、選法及行動由腳本驅動，本案提供其執行回執。可換後端的欄位映射由 GX 既有的 record-method-builder 支援。這一方法的實際輸入限定為此處聲明的書目欄位。[建置、發布、選法、讀數及兩次結題](field.json)

## 部署與重放

從產品根目錄在自己的部署安裝已發布的五檔插件。實際包摘要為 `87391be0a9860091dbb9ea7ca45c7e62169293d3db6faa088719692e1432e29d`。現場驗收使用產品相鄰 `../gx-science/asset-live/derived-wqm6fS/` 的單獨部署副本與獨立狀態資料庫。

```bash
bin/gx-p0 plugin install acceptance/science_assets/derived/package \
  --sha256 87391be0a9860091dbb9ea7ca45c7e62169293d3db6faa088719692e1432e29d
bin/gx-p0 plugin verify org.gx.record-coverage-6c93f523499b66fbf42e7ff4@1.0.0
```

隨包附入兩題的可攜記錄投影、凍結讀法和來源摘要。以下命令在新隔離部署重跑建置與兩次 GX 計算，無須連網。需要從原始供應商回應重建投影時，另執行最後一行；既有來源相對路徑及雜湊在 provenance.json。

```bash
work=$(mktemp -d)
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B \
  -m acceptance.science_assets.derived.run "$work" "$work/result.json"
PYTHONPATH=.:gx-h .venv/bin/python -B \
  -m acceptance.science_assets.derived.prepare ../gx-science/asset-live
```
