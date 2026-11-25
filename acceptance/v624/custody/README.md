# 第三份沉積的材料資格

**在 2026 年 11 月 25 日的公開查詢快照中，合格的新沉積為 0。** 兩份歷史參照都實際建模三條人源 TL1A 鏈。另五份同一靶標沉積的 model 1 各只有一條靶標鏈 A。兩份複合物的鏈 B 屬於 DcR3。由晶體對稱展開鏈 A，無法提供本題所需的三個獨立建模副本。盲碼、入組、科研開封和主端點計算均為 0。[原分派資格回執](receipts/eligibility.json)

保管者於 23:27:12 UTC 凍結目標、檢索、排序、排除及映射規則，首筆公開來源讀取在此後。以人源 TNFSF15 的 UniProt O95150、歷史編號 Q8NFE9 和三個名稱查詢。RCSB 的 TL1A、TNFSF15、完整蛋白名稱查詢分別得到 9、7、120 筆；與 UniProt、PDBe 取聯集共 122 個 PDB 條目。GraphQL 的 122 份條目記錄均已取得。其中 115 份的聚合物不對應人源 TL1A，包括 5YGP、5YGS 的受體 death domain。PDBe 舊編號路徑回傳 HTTP 404，現行編號映射和 reviewed UniProt 各自找出相同七個靶標沉積。[凍結規則](screening_protocol.json) · [全部候選及排除原因](receipts/screening.json) · [UniProt O95150](https://rest.uniprot.org/uniprotkb/O95150) · [PDBe 聚合物映射](https://www.ebi.ac.uk/pdbe/api/mappings/best_structures/O95150)

| 新候選 | 版本及版本日 | 繞射收集日期、光束線 | 實際靶標鏈 |
| --- | --- | --- | --- |
| [2QE3](https://www.rcsb.org/structure/2QE3) | 1.3，2023-08-30 | 2007-04-06，NSLS X29A | A |
| [2RJK](https://www.rcsb.org/structure/2RJK) | 1.3，2023-08-30 | 2007-06-29，NSLS X29A | A |
| [2RJL](https://www.rcsb.org/structure/2RJL) | 1.3，2023-08-30 | 2007-06-29，NSLS X29A | A |
| [3K51](https://www.rcsb.org/structure/3K51) | 1.5，2024-11-27 | 2008-11-08，APS 24-ID-E | A |
| [3MI8](https://www.rcsb.org/structure/3MI8) | 1.3，2024-11-27 | 2008-08-15，NSLS X29A | A |

這五份原件記錄的收集日期及光束線均異於兩參照的已記錄收集（2RE9 為 2007-03-07，2O0O 為 2006-10-22，兩者均 22-ID）。這個溯源欄位的比較對象僅為兩份參照。另兩個複合物的受體、個別構建體的變更及實驗標註均收錄於篩選表。兩個參照窗對齊五份新候選的聚合物序列，2RE9 label 21–40 與 101–120 分別對應 24–43 與 104–123。這是序列映射；三對鏈的觀測資格仍然缺少兩條鏈。這次僅審閱原件身分、取得、版本、序列、模型、靶標鏈標籤及占有率，沒有執行新目標的旋角、RMSD 或剔除計算。[逐位置映射及實驗標註](receipts/screening.json) · [來源查詢、原件雜湊與取得時間](receipts/archives.json)

若再有一份原始採集可驗、參照位置可映射且實際建模至少三條可比較靶標鏈的沉積，可按新查詢快照重新篩選。當前研究問題維持待驗，無法據此推斷三個鏈對的中位數會否掩蓋新樣本的區段例外。

## 重跑

在倉庫根目錄用鎖定的 Python 執行。完整封存內的聚合物檢索快照固定候選分母。查核器以原始 mmCIF 重新辨識實際模型與靶標鏈、對齊序列、核對每個來源雜湊，並按凍結規則重建原始篩選表。新查詢結果另立快照。重驗舊結果使用同一版原件。[封存檢索與聚合物原始回應](receipts/search-snapshot.zip)

```bash
.venv/bin/python -B acceptance/v624/custody/verify.py --work ../science-execution-validation/custody
```

乾淨機器可指定新保管目錄。額外取得的僅為五份原始座標檔，透過 Clash 的 HTTPS 代理，版本有變會列出缺少的原件。此命令不使用任何模型。兩份歷史參照原件隨倉庫交付。

```bash
.venv/bin/python -B acceptance/v624/custody/verify.py --work /tmp/gx-deposition-review --acquire
```
