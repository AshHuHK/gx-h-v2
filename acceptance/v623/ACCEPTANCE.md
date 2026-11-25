# V6.2.3 離線驗收與原始資料重評

在已部署的倉庫根目錄執行下列命令。第一行核對發布證據清單與五項判定。第二行從四個原始 GX SQLite、原生 Codex 會話及腳本通訊重建 24 份判讀文件。第三行從公開科學封包獨立重建四個 CPU 輸出、投遞與引用鏈。

```bash
.venv/bin/python -B acceptance/v623/validate.py verify --require-complete
.venv/bin/python -B acceptance/v623/public_replay.py verify --receipts acceptance/v623/evidence/live
.venv/bin/python -B acceptance/v623/study/archive.py replay --receipts acceptance/v623/evidence/study
```

四格重評會從本機 Git 物件庫讀取登記的裁決器版本，來源是 `82ee888158431c9031236b2e3642a50efcdbd1d0`。已有該版本 checkout 時，可以在第二行加上 `--source /absolute/path/to/checkout`。誤選已改動評分協議的 checkout 會收到 `Frozen scoring protocol changed`。[四格原始回執](evidence/live/replay.json) · [研究數值回執](evidence/study/replay.json) · [獨立複算](evidence/independent/result.json)

## 重新執行工程試驗

從具備鎖定的 Python、Codex 二進位與 Node 相依套件的乾淨候選 checkout 執行。令 `RESULTS` 指向倉庫外的輸出根目錄，`CLASH` 指向本機 Clash 的絕對路徑，`V622` 指向無改動的 `e6520f53ca19ef8eae3ec0915af74e9bfc6dcdca` checkout。各輸出子目錄須由指令首次建立。以下命令使用離線宿主。

```bash
.venv/bin/python -B acceptance/v623/campaign.py baseline --old "$V622" --work "$RESULTS/baseline" --clash "$CLASH"
.venv/bin/python -B acceptance/v623/campaign.py seed --work "$RESULTS/original" --clash "$CLASH"
.venv/bin/python -B acceptance/v623/campaign.py upgrade --seed "$RESULTS/original" --work "$RESULTS/upgrade" --clash "$CLASH"
.venv/bin/python -B acceptance/v623/campaign.py test --work "$RESULTS/regressions" --clash "$CLASH"
```

冷升級會讓同一個實體安裝路徑先跑 V6.2.2，再換到候選版本。兩次獨立程序重開同一個結題 Run，同一份原生報告重送兩次，最後回到原版和備份的 SQLite。資產內容、Job、model turn、結算和舊版政策投影均有比對。[冷升級](evidence/upgrade/check.json) · [回退](evidence/upgrade/rollback.json)

## 已知結果的版本歸屬

- `82ee888` 的 55 項新測試有 1 項夾具錯用了材料角色。將修正後的該案例交回完全相同的 runtime，原失敗案例通過。後繼 `0094e35` 的三項比較契約案例亦通過。[原始日誌](evidence/engineering/product.log) · [修正後的命令及 SHA](evidence/engineering/fixture-recheck.json)
- 既有探索測試共 646 項，642 通過，4 項由於未安裝選配 DSH SDK/runtime 而跳過。其他 V6.2.2 專項回歸為 37/37。11 組相容驗收中，原候選的 A7 錯把較新的 Goal 欄位送往舊版 CLI。後繼 `82bd5421` 使用舊版的原始欄位，在同一個 Run 完成冷升級。[跳過清單](evidence/compat/skipped.json) · [原分組結果](evidence/compat/groups.json) · [A7 後繼](evidence/compat/a7-successor/a7-attestation.json)
- 正式四格的原始硬額度與審查授權分母維持 0/4。四格的本地傳輸均有實際 Codex 會話，人工科學評分待審。原候選的 terminal SIGKILL 恢復成功，主動暫停後再恢復失敗。後繼 `30cc203` 成功恢復並得到零重複 Job、請求和結算的回執。[正式預檢](evidence/live/hard-preflight.json) · [原暫停失敗](evidence/live/pause-crash-command.json) · [後繼暫停恢復](evidence/live/pause-successor.json)
- 兩份公開結構、兩種遮罩的數值結論屬於已登記的回顧性 CPU 研究。原本的 CPU Run 使用腳本化研究者。獨立登記的真模型探索採用自己的 Run、審查安排與額度記錄。[PI 研究報告](V623_SCIENCE_REPORT.md)

本索引核對目錄中的工程、相容、冷升級、歷史封包、公開硬額度演練、獨立數值複算及 CPU 研究回執。另一協議的實際供應商試驗應沿用各自的註冊和簽收檔。[離線判定與檔案摘要](evidence/index.json)
