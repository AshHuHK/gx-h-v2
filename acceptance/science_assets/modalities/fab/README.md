# 兩份已發表 Fab 的 IMGT CDR 座標覆蓋

兩個小分子 Fab 的沉積原件均含一組可明確指認的重鏈與輕鏈。4FAB 是螢光素 Fab；1MCP 是磷酸膽鹼結合 Fab 的結構。以各自公開聚合物的可編號位置為分母，檢查兩條鏈六個 CDR 在第一個實驗模型中有哪些 Cα 座標。兩份原件有不同的序列、編號檔和位點分母。[4FAB 研究](https://doi.org/10.1002/prot.340050404) · [1MCP 研究](https://doi.org/10.1016/0022-2836(86)90245-7) · [原件、版本與校驗值](provenance.json)

| 實驗沉積 | VH 三區已觀測／已編號 | VL 三區已觀測／已編號 | 合計 |
|---|---|---|---|
| 4FAB | 8/8、10/10、9/9 | 11/11、3/3、9/9 | 50/50 |
| 1MCP | 8/8、10/10、13/13 | 12/12、3/3、9/9 | 55/55 |

方法從實際 mmCIF 讀取 assembly 和 entity，核對兩條不同的聚合物鏈及沉積者的 heavy/light 說明。具名編號檔將 ANARCI 的 H 或 K/L 域指派、聚合物摘要與該份 mmCIF 的解壓位元綁定。課題用現成編號檔啟動 CPU Job 即可。用不同原件的編號、交換重輕鏈角色或從裝配中移走輕鏈，方法分別回報無效配對。受控反例將輕鏈座標的佔據率歸零，4FAB 輕鏈得到 0/23，重鏈保持 27/27。

IMGT 指定 CDR1 為 27–38、CDR2 為 56–65、CDR3 為 105–117。ANARCI 由歸檔完整聚合物產生域位置對映。分母計算實際對到該域的聚合物位置，包含插入位置；編號對齊缺口跳過。分子與聚合物位置的核對採 `label_asym_id` 和 `label_seq_id`。每個位點只須有一個模型 1 中具有限座標與正佔據率的 Cα；altloc 不增加分子。結果描述這兩份沉積的觀測覆蓋；親和力、穩定性與配對正確率均未在這兩個結構上量測。[IMGT 官方區間](https://www.imgt.org/IMGTScientificChart/Numbering/IMGTIGVLsuperfamily.html) · [ANARCI 發行資訊](https://pypi.org/project/anarci/2026.2.13.2/)

五個測試覆蓋公開樣本、跨沉積編號、錯配、缺鏈及缺觀測。獨立讀取器使用另一份 PDB 固定欄位檔和作者編號，通過官方編號交叉表回對同一組凍結的 IMGT 位置；六區整數計數容差為零。歸檔資料與編號對映在兩種計算間共享。重新產生編號可使用記錄版本的 ANARCI 及 HMMER 3.4，所有 HMM 元件摘要可核對。普通 GX 執行僅需要隨題編號檔與產品既有 Biopython。[獨立數值結果](independent.json) · [測試原始日誌](tests.txt)

從產品根目錄重演離線檢查。

```bash
.venv/bin/python -B -m unittest -v acceptance.science_assets.modalities.fab.test_fab
.venv/bin/python -B acceptance/science_assets/modalities/fab/independent.py
```

目前方法支援明確標示兩條重輕角色、第一個模型、單一 identity 操作的 Fab 裝配，與來源相符的 IMGT 編號映射。多拷貝、非平凡的裝配對稱運算或不明確重輕鏈名稱需要額外配對資料。早期嘗試的 SAbDab 舊式 URL 實際返回 HTML；本件使用已記錄的 ANARCI 分派與 wwPDB 原件。[可重新執行的編號轉換](build_numbering.py)
