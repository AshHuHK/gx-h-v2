# V6 獨立工程與科學數值覆核

2026-11-25 UTC。獨立審閱程序直接開啟原生 GX 資料庫（SQLite `mode=ro`）、該專案的 CPU 執行記錄和產物、採納決策所屬官方會話的輸入與工具呼叫。此次由獨立 Codex 工程審閱者執行，無人類簽核。摘要由審閱者保存在產品目錄外。即時私有資料與審閱程序同屬一部受信任主機，這是獨立程序保管，沒有第三方簽章或提供商美元帳單。

| 樣本 | 原套件 verifier | 產品延伸 verifier | 獨立判讀 |
|---|---|---|---|
| 真實 2RE9 首次運行 #01 | FAIL，invalid evidence citation | PASS | 原始工具採納、觀測造成的主張修訂與關閉、九組數值重算均通過 |
| 真實 2RE9 凍結運行 #02 | PASS | PASS | 從 coverage-only 轉為逐鏈對幾何主張再關閉；獨立原件與科學數值均通過 |
| 首組盲測 p17 / p42 / p63 | FAIL，invalid evidence citation | FAIL，neutral metadata event changed science or commissioned scientific work | p17 通過，p42 通過，p63 失敗。整組未達標 |
| 凍結版三案重測 | FAIL，p17/p42 同回合工件引用被拒；p63 單案獲接受 | PASS | p17、p42、p63 科學判讀及生效行動均通過。旁支自然完成，決策期間同步執行未驗證 |

原 verifier 僅接受提交前已知的證據。採納前的同一 turn 實際產生了 claim 工件，observed、p17、p42 引用了其 ID。延伸 verifier 另查同一 turn 的原始產出與順序。兩份可執行程式與逐次輸出摘要均收入 JSON。這些新舊判定各自保留。

## 真實觀測 #01 與 #02

運行 `gxr_01m886be656v0peb41` 的首次及第二次決策任務簡報只有原始 2RE9、三個登記能力、題面和資源，均無既有的觀測或節點。首次原生嘗試沒有採納修訂。第二次的初始有效修訂提出三套選取都應有足夠覆蓋的假說並建立五個 CPU Job。最後一次 brief 包含五份源結果。官方 0.169.0／OpenAI／gpt-6.2-astra xhigh 的 call `call_ad892bc3b24844e3819613107b1f52e8` 對應決策 `gxa_8c9bcf2d01af3f1ad9cd2a89aa422e9eb74838fda28c037d5a19fb1485ada666`。它將原假說限縮為兩種全覆蓋選法，持久保存 mixed 主張 `gxh_01m886gvv7py5xdjwp@1`，並使運行在原生事件 122 進入 CLOSED。冷重開保留同一主張、五個 Job 和五筆提交。

獨立程序直接讀取原始 mmCIF，按模型 1、實體 1、非對稱單元 A/B/C 的位置、殘基及原子名稱配對，用 SVD 求正剛體旋轉。三條鏈共觀測到 164 處，沉積序列 1–17 無骨架座標。全骨架有 656 個原子，共有 Cα 有 164 個。兩者之間三鏈對的最大 RMSD 差是 0.039880913 Å，最大無號角度差是 0.064387560°，均出現在 B/C。凍結界線為 0.15 Å、0.5°。滿佔有率且各鏈骨架 B ≤40 Å² 的選法只有 49/164 處（29.88%），低於至少 60 處並達 60% 的要求。其 A/C 子集 RMSD 為 0.121846983 Å，只能描述選中的位置。對九個 Horn 擬合的最大重算誤差分別為 1.33×10⁻¹⁵ Å、8.53×10⁻¹⁴°，小於 0.001 Å、0.01° 的重算容差。單一沉積結構和共享鏈對無法確立跨樣本結論。[數值、位置、來源及 Job ID](geometry.json)

凍結版重跑 `gxr_01m8871dsn80ypeaz1`，使用相同 2RE9 原始位元組及方法。圖上先產生範圍限於覆蓋的 `gxh_01m8874e292t4sbxz9@1`。較晚的原生 turn 收到三個擬合及一項數值複算，另引用先前盤點。決策 `gxa_aab5331c6660cdc79f188f6ab8881f31f76528df1aa5e4067cafeeb5d27fda92` 保存幾何主張 @2（mixed），事件 131 使 run 進入 CLOSED，冷重開保持。49 處子集低於覆蓋要求，文字中其超限差值只可描述所選位置。從該 run 原始 DB 與封存 material 各自重算，九組誤差與 #01 相同。共 3 個提供商確認的模型呼叫、259,718 token、五個 Job、1.020 實測 CPU 秒、5 帳本 CPU 秒。兩次科學運行都用同一份沉積坐標。[凍結版原件比對](observed-02.json) · [凍結版獨立數值](geometry-02.json)

## 首組匹配對照

三個世界的原始五項輸入雜湊、目標、前態、可用工具、模型、設定和額度一致。每個 scoring turn 使用不同 run、thread、工作目錄，只接收自己包的原始觀測。逐一核驗了正式投遞的簡報、資料庫工件、實際送出的結構化決策、執行結果。三份宣告的可見檔案清單皆空，sandbox 為 read-only、停用網路。所有模型另可透過相同五種 GX 工具讀取各自授權的材料。溫度參數均未覆寫，提供商實際溫度值未披露。檢視通用注入與採納路徑時未發現由包 ID 指定結果的分支。這三個公開控制只能檢驗這三個已知干預。

| 包 | run ID | 原生決策與已執行效果 | 判讀 |
|---|---|---|---|
| p17 | `gxr_01m88663dpt5071t0n` | A/B 120 處、86%、0.12°、0.04 Å；A/C 112 處、81%、0.17°、0.03 Å。保存 A/B、A/C 的 limited_support，將 B/C 保持未定，兩個全鏈擬合均完成 | 通過 |
| p42 | `gxr_01m886629jfqd8pysh` | A/B 維持相同數值。A/C 為 112 處、81%、1.14°、0.31 Å，合乎覆蓋要求且兩項差值超限。保存 mixed 主張，保留 A/B 並辨出 A/C 敏感；兩個全鏈擬合均完成 | 通過 |
| p63 | `gxr_01m88660x3357hk1pg` | 檔名變而座標摘要相同。幾何主張仍 open，但新委任並完成兩個全鏈幾何 Job | 失敗 |

p17 與 p42 的實質差異在持久化主張。其兩個後續擬合的能力、協議和三鏈範圍相同。各世界的文獻 Job 在注入前已提交，保留同一 Job、operation key 和單筆收費，且均完成。第一組的 runner 在採集 before 後約 0.2 秒已完成，GX 也在 scoring turn 開始前對帳。已驗證自然完成與重開後的身份，對決策生效時仍在執行的旁支，第一組證據不足。每案完整 decision、thread、turn、claim、計費和 CPU Job ID 均在原件比對收據。[首組逐案收據](controls-first.json)

## 凍結版三案重測

首組失敗揭示後才登記此次完整重測，沿用所有三個公開干預與盲測邊界。候選版本 `fe7ff695` 增加前次決定及本次喚醒原因的脈絡。三世界同樣額度，每個世界各允許一次原生模型呼叫。目標、五個初始材料的 bytes、實際模型／工具設定、初始幾何判讀、保護 Job 的前態與原生呼叫均逐案核對。p17 及 p42 各作兩個全鏈擬合，實質差異在保存的主張。p63 採納 resolve，維持 open 且未派幾何 Job。

| 包 | run ID | 科學判讀及實際後果 | 確認 token／Job 數 |
|---|---|---|---|
| p17 | `gxr_01m8877nf3bx6p6b22` | 限 A/B、A/C 的 limited_support，B/C 保持未定；兩項對照的覆蓋與兩個變化量均達標。 | 64,222／3 |
| p42 | `gxr_01m8877nf8jemfrnxp` | mixed；A/B 在容差內，覆蓋合格的 A/C 兩個變化量都超限。 | 64,059／3 |
| p63 | `gxr_01m8877nejsd0ga69b` | 更名但摘要相同，保留舊主張及原計劃；只完成既有文獻 Job。 | 27,910／1 |

合計 156,191 個提供商已確認 token、7 個已結算 Job、0.484 實測 CPU 秒及 7 帳本 CPU 秒。既有索引各自然完成一次，實際 CPU 完成時間 02:05:57 UTC，早於三個 scoring turn 的 02:06:11–14。沒有物理並行的實證。冷重開後 p17、p63 的 driver 狀態 PAUSED → ACTIVE，所有三案保留科學 scope、主張、Job、operation key 及收費。原始 verifier 逐案為 p17/p42「invalid evidence citation」、p63 PASS；延伸 verifier 完整組 PASS。美元未知。完整 decision、thread、call、原始工件、時間和帳本身份均在收據。[凍結版逐案比對](controls-repeat.json)

## 資源與重放

首次 observed #01 共 3 個確認模型呼叫、353,579 個提供商 token，五個 Job 實耗 1.083 CPU 秒，帳本按每 Job 進位共 5 CPU 秒。首組三案共 9 個 Job，實耗 0.738 CPU 秒，帳本 9 CPU 秒。三個評分 turn 由提供商確認 130,952 個 token。另有三個額外呼叫，在原始重開快照未結算，之後同一 GX 計量表逐筆估計為 65,536，共 196,608 個估算 token。首組六次總入帳為 327,560 個 token。美元未知。保留原快照和後續計量狀態。[真實運行逐項收據](observed-01.json)

在產品根目錄、具備本地 Biopython 和 NumPy 的 Python 環境下，可直接從封存的位元組重算九個條目。

```sh
python -B acceptance/v6/reviewer/independent_geometry.py \
  --bundle acceptance/v6/observed-run-01/candidate-001 \
  --anchor ../reviewer-custody/observed-01.anchor.json
```

完整原件比對使用 `replay.py --bundle DIR --anchor FILE --kit DIR --window-state WINDOW_JSON STATE`，三案模式逐案重複最後一組參數。該程序僅讀取對應的原生 rollout、GX 記錄和產物，並分別執行兩個 verifier。首次稽核使用 `/tmp/gx-v6-protein-live-Tj8M3ONj` 與 `/tmp/gx-v6-controls-first{,-review}` 的新 run 資料；原件存續取決於其保管者。坐標重算亦從專案 DB 輸入重跑，與上列封存模式一致。
