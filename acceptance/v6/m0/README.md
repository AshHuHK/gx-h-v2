# M0 固定基線與發布方法複跑

2026-11-25 UTC 在兩個乾淨工作樹驗證 `gx-h-v2@e02df99757686982744813e5de745e86ef12e0a3` 與 `gx-h@23d581c27f46e65324246dff269b6ce77c9b3e9a`。`module_collaboration` 與 `gx_core/knowledge` 在兩倉逐一對應至相同的 Git tree。P0 基線的 `start` 選擇研究配方，空 workflow 的科學 reactor 提早返回，checkpoint 以已安裝 workflow 身份解析。這些是新增空圖決策入口的實際接點。[版本、初始盤點與環境](baseline-audit.json) · [符號與資料來源檢查](baseline-preflight.json)

| 本次執行範圍 | 結果 |
|---|---|
| Agent Kit 原件 | 49 個套件成員摘要吻合，V5 ZIP 的四個參照原檔逐位元相同，33 個 V5 案例按 ID 各有一筆 V6 去向 |
| V5 可相容性 | `--check-only` 完成 28 個增量項、28 個 SQL migration、131 個記憶體資料表，與編包輸出相同 |
| 三能力插件 | `gx-p0 plugin install/verify` 成功；在 `tl1a_geometry@3` 與 `module_collaboration@1` 綁定相同三個發行 pin |
| 包內開發檢查 | preflight 7/7、傳輸契約 6/6、含反例的匯出檢驗器 23/23 |
| 原產品路徑 | native 協作／記憶 2/2，P0 插件／租約／fencing／Engine 51/51，既有靜態蛋白 workflow 3/3 |
| 真實 CPU 方法 | 固定輸入、座標選擇與獨立 SVD 重算全部通過，冷重開重用四個原 Job ID |

實際的工作目錄、命令、耗時、退出碼與原始輸出摘要見 [命令清單](commands.json)。本機採 release lock 的 CPython 3.11.9、Node 22.17.0、npm 10.9.2、Biopython 1.88、NumPy 1.26.4。複跑腳本從本機 Git 固定物件建立隔離環境，透過 `--no-index --require-hashes` 安裝原 wheel。 [插件綁定](plugin-smoke.json) · [V5 合約回執](v5-compatibility.json)

## 2RE9 真實重算與資料範圍

套件指定的全新 Run `gxr_01m885vthd28bbykes` 執行一次盤點和三個測量，取得九組獨立重算。同一沉積結構上，共同主鏈與共同 Cα 在所有三對鏈均符合既定比較容差。有序主鏈只有 49/164 個位置，低於 60 個位置及 60% 覆蓋的事前門檻，三方案合判為 `insufficient`。以相同原始測量改為 0.01° 門檻的負控制得到 `sensitive`，另外故意改一度，獨立重算檢出不一致。共同評分集合的探索性分析亦已從原始座標複跑，並核對六組對角評分和六組殘差分解。這批證據來自同一個晶體模型。 [真實測量、差異及容差](2re9-fresh.json) · [共同評分結果](common-score-fresh.json)

編包機使用 CPython 3.11.18，release lock 指定本機使用的 3.11.9。實測科學欄位相同，四份輸出各自帶有 runtime 版本，且三個幾何結果各引用盤點的摘要，因此輸出 SHA 有變。在記憶體副本替換這兩項即可逐一還原全部四個編包 SHA。原始產物保留其實際執行環境。[兩輪 SHA 及比對範圍](baseline-audit.json)

原腳本會清理臨時 DB 和 Job 原件。為保存可獨立核對的資料，另從固定 `git archive` 啟動 `acc1_coordinate_method.py --copied-root`，在第二個新 Run `gxr_01m885x8krsktgwkqf` 保留資料庫、四份原產物及四份 Runner outcome。這次四份原產物摘要與第一個新 Run 完全相同。`PRAGMA quick_check` 返回 `ok`。四項 Job 均結算，GX 合計扣 4 CPU 秒與 4 筆 submission，預留餘額為零；Runner 累計 CPU 為 0.322 秒，GX 各 Job 獨立向上取整。兩次方法複跑共啟動八項 CPU Job，冷重開各新增零項。沒有付費模型請求。未設定本地 CPU 的美元單價。 [原件索引、摘要及成本](2re9-source-records.json) · [原始資料庫與產物壓縮包](2re9-source-records.tar.gz)

從解壓的 Agent Kit 根目錄重跑，`GX_REPO` 為含固定提交的產品工作樹，輸出路徑須為新檔案。

```bash
python3 -B tools/preflight.py --repo "$GX_REPO" --native "$GX_NATIVE_REPO"
python3 -I -B tools/smoke_plugin.py --repo "$GX_REPO"
bash protein/baseline/reproduce.sh "$GX_REPO" "$PWD/recheck.json" python3.11
python3 -I -B protein/reviewer/common_score.py "$GX_REPO" "$PWD/recheck-common.json"
python3 -I -B protein/reviewer/verify_common.py "$PWD/recheck-common.json" "$PWD/recheck.json"
```

## 歷史對照

`acc_1/summary.json` 自述為 `p0-grid`，其實際 CPU 計算為 fixture-grid。歷史蛋白 OAuth Run `gxr_01m816989yd1bcmpyp` 的正式紀錄在 `acceptance/runs/acc-1-research/oauth-v2/`。該 Run 以 49 個位置及 49/164 的門檻得出 `sensitive`。V6 的固定基線按自己的 60/60% 判據重評同一類原值。這兩份歷史紀錄只用來查已花費用、已讀資料及舊判定的適用範圍。M0 的新 Run 使用本機計算，Lead 的自主選擇與官方模型判斷由 M2 另行核驗。

P2A.1 的全量 38/38 和 hardening 13/13 保留歷史標籤，原 42 個失敗的身分與因果對照見 `P2A1_HARDENING_REPORT.md`。本輪只重跑表中的 M0 相關測試。編包時另記載系統 Python 3.12 下的四筆幾何斷言；本輪鎖定環境中的靜態幾何三測均通過。舊 3.12 觀測保留原環境標記，不能以本輪結果逐項歸因。
