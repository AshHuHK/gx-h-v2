# S288C 公開天然酶原始資料複核

使用麵包酵母 S288C（NCBI Taxonomy 559292）已歸檔的條目，核對「反應映射、EC 類成員、實驗資料庫收錄」各自的分母。這是離線唯讀驗證。欄位中 `0` 表示在指定來源與範圍內查無記錄。缺少特定底物標籤、測定單位或菌株資訊時，其生物學意義保留為未知。[逐項資料、摘要與來源行號](reference.json)

| 讀取問題 | 麥芽糖 | 異麥芽糖 |
|---|---|---|
| Rhea master 與正向 ID | 68796 / 68797 | 68864 / 68865 |
| Rhea 全物種映射列 / 與 6,733 個 S288C accession 的交集 | 8 / 0 | 8 / 0 |
| 此 Rhea 反應明載的 EC | 3.2.1.48 | 3.2.1.10 |
| 獨立查詢的天然酶類別及 S288C 成員數 | EC 3.2.1.20，2 個 | EC 3.2.1.10，5 個 |
| 上述 EC 在本地快照所有物種的 BRENDA / SABIO-RK 列數 | 1,405 / 162 | 367 / 8 |
| 上述 S288C 成員以明列 accession 命中的相異 BRENDA / SABIO-RK 條目 | 2 / 0 | 107 / 0 |
| BRENDA 儲存值明列精確化合物名稱的條目，分為單一 accession / 分組 | 0 / 0 | 12 / 2 |
| 上列化合物在 Km、turnover 或 kcat/Km 催化參數中明列為底物，單一 accession / 分組 | 0 / 0 | 10 / 1 |

酵母麥芽糖酶 MAL12 與 MAL32 是 EC 分類下的命中。原始反應來源列出了另一 EC。兩種來源各自有依據，合併報告時必須保留這個差別。MAL12 的兩筆 BRENDA 條目來自同一 PMID，一筆指向其他底物，另一筆在值中沒有明列底物。因此仍缺直接對應本題底物的有定位讀數。異麥芽糖的 107 條去重記錄中有 15 條標記 grouped，逐 accession 相加會重複計數。首次核對的 14 條計的是 BRENDA 儲存值中明列異麥芽糖的化合物標籤。再按測定終點核對，其中 3 條是抑制常數 Ki（3.2.1.10/ki_value/17、/21、/24），其餘 11 條屬於 Km、turnover 或 kcat/Km 底物記錄。這次未逐篇閱讀全文。重複的論文、別名、不同參數和分組記錄均不能直接算成獨立重複實驗。這些結論描述收錄範圍，沒有新測酶活。

## 實際讀取的原件

五個 SQLite 分別是 Rhea 142 / Swiss-Prot 2026_03 關聯、ENZYME 28-Jan-2026 / Swiss-Prot 分類、Rhea 名稱及方向對照、BRENDA 2026.1 / SABIO-RK 2026-09-20 測定，以及已選 Horizyn 特徵的索引。報告保存每個檔案的 SHA-256、表名與列數。最後一個索引只核對檔案、內部表及來源識別；本次推論和活性預測次數為零。[快照與表結構紀錄](reference.json)

獨立讀取器解析 Rhea 原始壓縮包的 `rhea2uniprot_sprot.tsv`、`rhea2ec.tsv` 和 ENZYME 原始 `enzyme.dat` 的 DR 記錄。它逐筆核對 FASTA 中的 accession、長度與上述七個天然成員的序列摘要。NCBI BLAST 的 `blastdbcmd 2.17.0+` 實際讀回 V5 資料庫全部 6,733 個 accession 與長度，共 3,025,957 個殘基，與 FASTA 全數吻合。動力學記錄只有精確 accession 歸屬才納入本菌株成員的計數；其來源實驗未必記到菌株層級。原始檔授權依各提供者和既有部署紀錄，本文交付的是摘要、數量與定位。

## 重算

從 `gx-auto` 工作區執行。需要 Python 3.11 或更新版本和已掛載的資料。若搬移上游平檔，另傳 `--rhea-tsv` 和 `--enzyme-dat`。省去 `--blastdbcmd` 時僅比對 FASTA 與 BLAST manifest 的數量，沒有宣稱實讀 BLAST 索引。

```bash
case_dir=gx-v-6/product-multi-workflow/acceptance/science_assets/enzyme/sources
python "$case_dir/verify.py" \
    --asset-root gxdesign-harness/releases/science-native-v2/var/enzyme-assets \
    --blastdbcmd ../conda/envs/auox8-core-v14/bin/blastdbcmd > /tmp/gx-s288c-source-audit.json
cmp "$case_dir/reference.json" /tmp/gx-s288c-source-audit.json
```

兩套不同路徑也經實物測試，第二份在暫存目錄中有獨立複製的五個 SQLite、FASTA、BLAST 元件和兩個上游平檔。第二次輸出與原來逐位元相同；將第二份 ENZYME 平檔替換為其他內容後，核對確實拒收。[搬移與變更負例](relocation.json)
