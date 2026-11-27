# 公開肽沉積的覆蓋與化學註記

典藏人員要比較兩份 NMR 結構是否都有完整肽主鏈時，應同時查看聚合物位置和胺基酸位置兩個分母。這裡的計數描述 wwPDB 已沉積的構象。NMR 模型數是同一沉積的構象數，沒有逐位點的模型預測置信度。[1UAO 原著](https://doi.org/10.1016/j.str.2004.05.022) · [1LE0 原著](https://doi.org/10.1073/pnas.091100898)

| 沉積、鏈 | 構象 | 有可用坐標的聚合物位置／宣告位置 | 具有完整 N、CA、C、O 的肽位置／明示胺基酸 | 化學解讀 |
|---|---:|---:|---:|---|
| 1UAO A | 18 | 10/10 | 10/10 | 聚合物清單包含十個常規胺基酸。 |
| 1LE0 A | 20 | 13/13 | 12/12 | 第 13 個位置為 NH2 基團。原件的 covale1 將它連到第 12 個胺基酸。將兩個分母混用會誤報缺失一個主鏈。 |

兩份原件都缺少獨立的 modified-residue 註記類別；其他未聲明的修飾保持未知。完整 mmCIF 來自 2022-09-28 快照。另一份 wwPDB 固定欄 PDB 由獨立讀取程式復算，同時核對 38 個構象的整數計數、位置與末端連接。它們提供同一次實驗的第二種檔案表示。[檔案來源、版本及 SHA-256](sources.json) · [獨立讀數與舊方法的原始差距](numerical-check.json)

方法 `archival-peptide-coverage@1` 接受實驗沉積的一個短多肽 label chain 和 2–64 個已宣告聚合物位置。它處理合適佔據率、重原子、實際坐標及同一構象內相容的替代位置，保留非標準胺基酸、其他化學組分、有來源的修飾及化學分類未知的位置。工具遇到多肽鏈數不符、微異質位點或資料內身份互相衝突時會回報輸入不適用。輸出為 `peptide-qc.json`。實驗坐標本身不足以求預測置信度、能量、溶液中族群比例或生物效力。

對於顯式首尾閉環，已有的 `deposited-macrocycle-qc@1` 讀取原件 `_struct_conn` 和端點坐標。6BEN 公開沉積的 20 個構象都有可比較端點，原件有一項明示閉環；人工刪除該條聲明後，幾何逐模型相同，明示閉環欄位為零。這項回歸驗的是已沉積拓撲的讀回。環肽新構象預測仍未驗收。[6BEN 原件、單改動反例及判據](../../../science_capabilities/structure_controls/README.md)

在產品根目錄可做離線復算。

```bash
.venv/bin/python -B acceptance/science_assets/modalities/peptide/verify.py
.venv/bin/python -B -m unittest -v acceptance.science_assets.modalities.peptide.test_peptide
```

五項定向回歸涵蓋兩個正例、化學資料未知、不同替代構象、修飾的鏈範圍，以及原有環肽顯式連接對照。[首次缺口](baseline.log) · [修後測試](tests.log) · [普通 Goal 題面](../studies/linear-peptides/question.md)

首次普通 Goal 的設定逐位元保存在[線性肽首案](../studies/linear-peptides/first-goal.json)與[環肽首案](../studies/macrocycle/first-goal.json)。新題的 [線性肽設定](../studies/linear-peptides/goal.json)及[環肽設定](../studies/macrocycle/goal.json)對三份實驗 mmCIF 隨材料登記來源、修訂 1.3、原文 DOI 與檔案 SHA-256，採樣獨立性保留未知。6BEN 刪除閉環聲明的檔案保留衍生對照身份。這項補登僅適用於新建 Run，首次決策與原始收據仍按當時入場條件解讀。
