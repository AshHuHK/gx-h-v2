# GX-H V6.2B 科研入口

V6.2B 延用 `bin/gx-p0` 與每項研究自己的 state。Codex Lead 依可用方法、已發布報告和剩餘額度決定接下來的工作。模型推理檔位、研究 effort 和資源預算可分別設定。固定版 Python、Codex 及 Cordis 的離線安裝見 [V6.1 部署](V61_DEPLOYMENT.md)。

## 建立一項研究

在此版本根目錄使用隨附的公開授權範例。將 `study` 換成這一項研究專用的目錄。以下步驟會建立空圖及首項決策任務，可在開始推理前查看。

```bash
source .venv/bin/activate
bin/gx-p0 plugin verify org.gx.research-survey@1.0.0
study="$(dirname "$PWD")/studies/deposition-study"
bin/gx-p0 start --state "$study" --run-key coverage-study \
  --goal '比較兩份沉積結構在不同選取方式下的座標覆蓋。哪些判斷有數據支持？' \
  --goal-config examples/plugins/v62_public_materials/grant.json
bin/gx-p0 status --state "$study"
```

正式研究以 `bin/gx-p0 run --state "$study" --timeout 600` 驅動，原生推理和 Job 受該 Run 的額度限制。操作員暫停過的研究可用 `bin/gx-p0 resume --state "$study" --timeout 600` 接續。公開範例隨附的短文及合成 JATS 供工程驗證使用，原始座標和方法資料各自標明來源。[公開材料與授權](examples/plugins/v62_public_materials/README.md)

| 研究 effort | Lead 可選工作 |
|---|---|
| `quick` | 直接使用授權的方法或既有流程，預設零研究模組。 |
| `medium` | 可委派資料盤點，以及模型、workflow、評測資源盤點。 |
| `deep` | 另可委派離線文獻與機制分析、假設空間研究。Lead 可只選一項，或以已有證據結題。 |

在 Goal 配置中以 `research_modules` 指定模組版本、`capabilities` 指定可執行方法、`input_files` 指定授權材料。目錄中的模型卡、workflow 和評測規格供調研使用；實際調用歸該 Run 的執行授權。額度下撥、Job 去重、模組完成後重啟與部分報告沿用 A 的執行入口。[A/B 介面](V62A_MODULE_HANDOFF.md)

## 如何解讀結果

- 文獻報告可追到原始檔雜湊、段落及原文。正式評審使用另一個工作者自己的讀取收據。相反解讀和待解決的缺口隨同保存。
- 主張有兩個不同欄位。方向描述證據支持或反對什麼；成熟度說明它目前是構想、有據、已預登記、已檢驗或經獨立重複。已檢驗的預測也可能被結果否定。獨立性取決於取得資料、樣本和來源的紀錄。
- 在下一輪凍結簡報中，Lead 看得到已發出的工作、已交付報告及可引用的結果。達到設定的連續無進展輪數後，Lead 可依現有證據決定收斂或結題。在途工作仍保留等待條件。
- 選擇收斂會保存理由、候選、引用與規格版本，並關閉普通探索模組的入口。正式封存終評和報告簽署屬於 V6.3 的交付。

## 提交實測資料

若研究需要比較事前預測與後來的實測值，操作員可提供讀數檔及取得資料時的來源說明。數值保存在讀數原檔；樣本、資料取得、處理、取樣和適用範圍保存在 acquisition 檔。GX 會把兩者綁定到同一筆觀測，供後續採納和重評使用。

```bash
bin/gx-p0 observe --state "$study" --run-id "$run_id" \
  --source sensor-a --input reading.json --acquisition acquisition.json
```

可比較的實驗規格、兩個不同來源給出相同讀數、重複提交與多輸出 Job 的範例見[產物分類測試](gx-h/tests/test_v62_outputs.py)及[主張成熟度測試](gx-h/tests/test_v62_maturity.py)。

## 升級、續跑與回退

既有 Run 沿用建立時固定的模組、方法及資料版本。B 的證據協議在新建的 medium、deep Goal 保存版本標記。舊狀態可用原有回執對帳；原先的主張、採納條件與費用保留。升級前在寫入停止後保留完整 state、一致的 SQLite/WAL、產物、宿主會話及舊執行環境。回退使用相配的原版本與快照，先對帳升級後已受理的外部工作與扣費。[原有升級與回退步驟](V61_DEPLOYMENT.md)
