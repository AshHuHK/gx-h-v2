# 歷史科研資產與本次查核

已在共享部署找到盤點原件，與附件逐位元核對了 README，SHA-256 是 `f206617070aed29dcbb56f0d67ab987a661c377698f06d7beb103614f1e3d9c8`。七份原表在邏輯掛載 `historical-science-inventory`，當地位置為產品目錄相對的 `../gx-science/historical-capabilities`。表中原始定位起於邏輯掛載 `ash_proj`。產品隨附小型卡片索引，可在歷史掛載離線時向 Lead 展示已核過的資料。[表頭、完整 SHA、去重及各狀態分母](summary.json) · [完整逐列賬本](ledger.jsonl) · [可部署目錄卡](../../../gx-h/config/science_asset_inventory.json)

| 原表 | 本次實讀列數 | 不同表內身份 | 不同「原位置＋文件內定位」 | 本次核到什麼 |
|---|---:|---:|---:|---|
| research.tsv | 26 | 26 | 20 | 11 個文件定位、15 個目錄定位。多個資料來源可共用一個舊供應器。 |
| mcp-servers.tsv | 117 | 117 | 117 | 116 份工具目錄的 server ID 與所列工具數全部逐一吻合；另有 1 個元件僅在登記冊出現，原表工具數為 0。 |
| mcp-tools.tsv | 958 | 958 | 958 | 逐一在快照中解析對應 JSON pointer 並核對操作名、server ID；全部一致。 |
| workflows.tsv | 192 | 192 | 192 | 160 個 JSON pointer、32 筆文件定位。含準備階段、運行路線及延期案例。 |
| methods.tsv | 164 | 161 | 164 | 149 個 JSON pointer、15 筆文件定位。行 25/26、29/31、38/39 各共用一組歷史身份，各有各自的原件。 |
| data.tsv | 145 | 145 | 145 | 46 個 JSON pointer、22 筆普通文件及 77 筆目錄定位。目錄可讀的項目仍需資料版本、樣本或查詢證據。 |
| skills.tsv | 554 | 554 | 554 | 554 個指向實際可讀文件的定位。作者／知識包／展示集合保留在原表。 |

完整列摘要均相異。2,156 行各可用「表名、從 1 起算的物理行號、原表 SHA、正規化完整列 SHA」定位。賬本內的歷史身份含原表自己的識別欄位；這個層次不等於跨供應器去重後的科學方法。表格狀態描述主定位，未逐筆開啟每個選填的備註、附件或二級運行回執。對真正取得的科研結果，請另追 Goal、Job、輸入和產物。

## 目前已進入 Lead 目錄的內容

102 張卡對應 62 筆 MCP 讀取操作、32 份資料目錄／快照、4 個方法、2 條歷史路線、1 個讀取技能及 1 個研究來源。每張有共享操作的 `canonical_method_id`、歷史提供者 `provider_binding`、版本或未知、輸入、可得輸出、適用界線、原表與原件依據。本批卡均為 `reference_only`。例如 PDB MCP 和 RCSB 路線共用 `pdb-archive`，同一沉積透過兩個入口仍是同一科學來源。Protenix 的登記固定到歷史 revision，能檢視模型身份。卡內的歷史提供者資訊不會授予現行 Goal 的 Job 權限。

| 領域 | 可查原件 | 可驗收範圍 |
|---|---|---|
| 公開蛋白 | PDB / RCSB 兩種資料入口、單筆沉積質控操作、舊三鏈幾何方法、PDB 閱讀指南及歷史結構模型卡 | 舊三鏈方法只涵蓋同一實體的觀測坐標。資料歸檔和模型預測另記。 |
| 酶 | Rhea 反應別名、Rhea/Swiss-Prot、BRENDA/SABIO-RK 快照及歷史動力學讀取方法 | 實際驗讀了五個 SQLite 的目錄結構。另在指定釀酒酵母反應核對三筆天然蛋白註釋；個別紀錄的催化方向皆為 UN。動力學檔有 10,818 筆 BRENDA、1,872 筆 SABIO-RK assay。資料列數不等於獨立實驗數，也不能證明這三筆註釋的活性。當地原 manifest 指定此 SABIO-RK 匯出為學術使用；本交付留摘要與版本，完整資料留在掛載。[實際唯讀資料結果](snapshots.json) |
| 線性多肽 | PDB Chemical Component Dictionary，PDB 沉積和質控入口 | 可確認歷史資料目錄可讀；長度、修飾完整性和模型置信度需要按具體方法驗證。 |
| 環肽 | 歷史環化連接質控案例 | 首次閉環距離未通過原先登記的準則，保留歷史陰性。 |
| 抗體 | 歷史 Fab 成對覆蓋缺口、PDB 存檔資料 | Fab 舊案例明載延期。新 Fab 資料上的配對、編號及 CDR 覆蓋需要其自身的測量。 |
| 資訊來源、RNA | Europe PMC 原文路線、歷史 EternaFold 模型 | 文獻檢索命中可只有摘要。RNA 僅作索引。 |

另外 2,054 行保留各自處置，分為 1,011 行歷史 MCP、912 行一般索引、81 行延後的 RNA 索引和 50 行跨題來源參考。117 個 server 元件保留為部署資料。958 個 MCP 操作中，62 個已整理為可讀取的歷史操作卡。另有 229 個一般作業生命週期操作、140 個會修改狀態的操作、1 個任意命令入口、126 個尚未逐操作判明適用範圍的資料庫／文獻候選、400 個未取得只讀操作審查的其他項目。每行都有具體處置理由；原有獨立插件在其他課題的結果仍歸原案。尚未整理概念的 `canonical_method_id` 為 null，歷史身份摘要仍可追溯。領域標籤彼此可重疊，102 張使用人工核選或已釘住的來源操作身份，其餘從身份、來源說明及文件指引抽取。725 行原始描述未能可靠歸類，留作 `unclassified`。完整運行證據只由相應課題產生。卡片涵蓋範圍與索引領域詞彙可在程式中核對。[卡片選擇](selection.py) · [解析及覆蓋定義](build.py)

## 具名操作和現行入口如何對上

62 個 MCP 歷史操作均以 server ID、operation 名稱與預先核選的整個操作定義 SHA 固定。逐條檢查舊 catalog，列出真實輸入欄名、必填欄、輸出 schema 的有無及其欄名。19 個操作有正式輸出 schema；其他 43 個缺少這項資料，保留未知。服務器的一次握手或某一種輸入取得過成功回應，不會改變其他操作的運行狀態。公開來源的範圍包括 PDB 沉積、AlphaFold DB 既有預測的覆蓋／置信度記錄、UniProt 註釋、KEGG 的反應與酶分類、已發表抗體試劑的檔案註釋、PubMed/Europe PMC/arXiv 等文獻檢索及原文。這些卡不包括現場任意命令、模型生成、提交作業或更新外部資料。[逐操作選擇與資料庫來源](read_operations.py) · [舊操作定義摘要](approved-operation-digests.json)

28 張卡另帶 `native_relation`。生成時須在版本化插件登記和該插件的方法 manifest 同時核到方法 ID、版本；每條連結帶入 package／manifest 摘要及具體適用範圍。九種可查的具名方法包括 P12004 的 PCNA 沉積查詢與取件、S288C 的反應註釋和蛋白記錄、專屬酵母快照查詢、文獻搜尋與取原文以及 arXiv 搜索與取件。公共資料源和這些窄域方法的名稱、資料範圍和提供者各自保留。例如 Human Protein Atlas 的人類組織記錄無法用只收酵母蛋白登錄號的接口代查。Lead 仍需以該 Goal 的原有目錄選用已登記、與當前材料相符的方法。

## 復現

在完整產品根目錄執行。輸出目錄可另選；對比相同輸入時，生成的目錄與逐列賬本可逐位元比對。

```bash
python3 acceptance/science_assets/inventory/build.py \
  --asset-root ../../.. --table-root ../gx-science/historical-capabilities \
  --output /tmp/gx-h-inventory --cards /tmp/gx-h-inventory/cards.json
python3 acceptance/science_assets/inventory/snapshots.py \
  --asset-root ../../.. --table-root ../gx-science/historical-capabilities
python3 -m unittest -v acceptance.science_assets.inventory.test_inventory
PYTHONPATH=.:gx-h .venv/bin/python -B -m acceptance.science_assets.inventory.catalog_probe
```

六個針對性測試驗證更換兩個實際根目錄後結果仍一致、原件缺失保留逐列身份、工具在舊 JSON 陣列裡換位置時不誤發卡、同名操作的契約更動須重新核定，同身份原行留存與歧義偵測，以及有操作列表與僅登記服務的證據差異。原表缺失時分母記為未知，其餘六表仍可處理。首版普通 Goal `gxr_01m8fdm18y57x1tk79` 對最初 15 張卡完成 30 次精查。擴充後摒除不同資料來源的錯配，普通 Goal `gxr_01m8fdypax0yvjxemv` 在受控原生傳輸下分別用跨類及專類精查全部 102 張，204 次查詢均傳回原出處、正確摘要與 `reference_only`，0 次模型推理、0 個 Job。這個驗證回答「已進入目錄且能讀出詳情」。[定向測試](test-results.txt) · [最初 15 張的回執](catalog-probe-first-15.json) · [完整 102 張的普通 Goal 查詢回執](catalog-probe.json)
