# GX-H V6.2B 驗收

本版用正常 Goal 入口觀察一個完整的科研決策：Lead 選工作，研究模組取得被授權的材料並發表報告，下一次 Lead 從實際送達的簡報引用報告，決定接下來的動作。V6.2B 的執行與數據驗證提交為 `530b1380a1e3bb3cd9ccb5023d436c708512113a`。基底為 A 的產品提交 `3bfca71` 和驗收提交 `ba1a526`；B 分支是 `v6.2b-evidence`。[A 的驗收及完整提交](V62A_ACCEPTANCE.md) · [使用本版](V62B_DEPLOYMENT.md)

## 驗收結論

| 驗收項目 | 狀態 | 分母與收據 |
|---|---|---|
| 平台與科研規則回歸 | PASS | 內核測試發現 601 項，通過 597，另有 4 項選配 DeepSeek SDK 測試跳過。A 模組生命週期 19/19、B 輪次與收斂 8/8，最終配對規格工程控制 8/8。 [逐組日誌與摘要](acceptance/v62b/receipts/regression/result.json) |
| 公開腳本全鏈演練 | PASS | quick、medium、deep 改路、deep 維持共 4/4。6 筆實際 Job，3 次報告喚醒，6 次重送。 [逐案收據](acceptance/v62b/receipts/engineering/summary.json) |
| 冷重啟、競態、升級及回退 | PASS | A1–A7、原 A7 升級，以及 7 筆歷史 Job 再開。 [脫離 Git 的再驗](acceptance/v62b/receipts/compat/detached-verification.json) |
| 獨立科研規則工程複核 | PASS | 四個正常 Goal 情境、五個數值反例及 42 項聚焦測試。各組存在覆蓋重疊。 [逐項觀察](acceptance/v62b/receipts/scientific-controls/results.json) |
| 未見案例上的真模型決策 | NOT_RUN | 應改路 0/1、應維持 0/1；兩個名額均未進場。 [實驗狀態](acceptance/v62b/study/not-run.json) |

## 公開材料上的決策

兩個 deep 案例共用問題、起始材料、能力、模型設定與額度。Lead 首輪留下比較主鏈覆蓋的暫定方案。測試只改了盤點工作者在第一份沉積結構上採用的選取規則。GX 保留每次送達的簡報、來源、修訂、實際 Job 與回執。下列比例的分母是該沉積結構中此分析能觀察到的位置數。

| 公開案例 | 模組交付給 Lead 的資訊 | 查到的後續工作 |
|---|---|---|
| quick | 零模組，直用已授權的方法 | 2O0O 的共用主鏈覆蓋 152/153。 |
| medium | 方法調研交付適用方法、來源及排除理由 | 執行入選方法，2O0O 共用主鏈覆蓋 152/153。 |
| deep，應改路 | 2RE9 的有序主鏈只覆蓋 49/164，低於預設的 60% 門檻 | Lead 引用盤點結果、關閉原分支，另派 2O0O 共用 Cα 覆蓋量測，得到 152/153。 |
| deep，應維持 | 2RE9 的共用 Cα 覆蓋 164/164 | Lead 引用結果、保留原分支，按原方案執行 2O0O 共用主鏈量測，得到 152/153。 |

四個案例完成 6 筆 CPU Job，實測 CPU 時間合計 0.505 秒。Run Lead 共 11 輪，收到 72,984 位元組凍結簡報；模組共 5 輪，收到 21,554 位元組。腳本記帳以 input / output / cache 表示，Lead 為 540 / 90 / 0，模組為 240 / 40 / 0。逐案歷時、決策引用、費用欄與重送紀錄可查原始回執。[四案機讀摘要](acceptance/v62b/receipts/engineering/summary.json) · [獨立配對判讀](acceptance/v62b/receipts/engineering/public-check.json) · [含官方宿主紀錄的公開證據包](acceptance/v62b/receipts/engineering/public-evidence.tar.gz)

以上為本機腳本提供的模型回覆，經固定版本的官方 Codex App Server 送達。計算由真實 CPU Job 執行，結果由原始座標獨立複算。每個含報告的案例均在冷重啟後重送兩次報告；原有接收、喚醒、決策、Job、成熟度和帳務可逐項比對。舊座標能力缺少產物性質宣告，因此本研究的數值支持有界的座標描述。其採納主張維持保守的成熟度。[公開研究與重播](acceptance/v62b/engineering/README.md)

## 證據與操作邊界

- 同一筆實際 CPU 工作回傳分析均值 3 與模型預測 7。依事前登錄的門檻 5，前者反對預測，後者支持對模型輸出的描述。每筆結果保存對應的能力版本、命名輸出、輸入與收集收據。[多輸出案例](gx-h/tests/test_v62_outputs.py) · [成熟度判定](gx-h/tests/test_v62_maturity.py)
- 來源證據須能定位到對應版本與段落。第二位工作者需用自己的身份和讀取收據複核。相反或未決解讀可保留。相同樣本的分樣、重算與共同來源不能建立獨立重複。相關已知讀數在首次登錄前已存在時，也不能取得事前登錄資格。尚未發布的資料會影響相關判定，但其內容仍維持原有可見範圍。[來源與評審測試](gx-h/tests/test_v62_sources.py) · [跨模組交接測試](gx-h/tests/test_v62b_handoff.py)
- 在途工作、未決審批與重複發布均能和真正結算的無進展輪次區分。可配置連續無進展輪數；達到門檻後交給 Lead 選擇收斂或結題。普通新探索入口會依收斂決策關閉。關鍵反證可提早喚醒，晚到的新結果會送達下一份簡報。[輪次與收斂驗收](acceptance/v62b/test_rounds.py)
- 同一份四模組、三方法的目錄，在此次對照中使用類別摘要後，送達簡報由 4,909 位元組降為 3,419。兩種呈現都能找回相同的評測調研身份，分頁可發現全部四個角色。另一個小型示範插件經標準入口安裝後，內核檔案的內容摘要一致。[模組與目錄測試](gx-h/tests/test_v62_modules_catalog.py) · [獨立示範插件](examples/plugins/v62_reading_check/plugin.py)

## 版本相容與復原

B 從 A 讀入範圍化採納、局部工作額度、模組報告、喚醒和復原紀錄，繼續使用原 Goal、Job、租約與預算所有者。A 的合同摘要為 `3a91341b8976e142d24f84c6ddb567b4e56e9febe8794bad4f75be79bbd9f939`，B 的合同摘要為 `f2923197c830297321b06cdf364bd8be1e6066630d5a927b683c327e96688e0d`。每項研究固定當時可用的方法與模組版本。[A 模組合同](V62A_MODULE_HANDOFF.md) · [B 合同檔案](gx-h/contracts/CONTRACT_SHA)

在 B 的固定提交上重跑 A1–A7 與原 A7 升級程序，包含三種圖頭競態、部分交付及工作完成後崩潰。兩次 B 冷啟動後恢復舊版，可核對 5 筆 V6.1 和 2 筆 A 的歷史 Job；兩筆待收集結果各只結算一次，原輸入檔離線時仍可恢復 A 的已發布報告。從無 Git 目錄的交付副本再驗，28 個證據檔與 328 個 B 執行檔案通過核對。[相容性收據](acceptance/v62b/receipts/compat/index.json) · [獨立副本結果](acceptance/v62b/receipts/compat/detached-verification.json) · [重播步驟](acceptance/v62b/compat/README.md)

## 真模型科研驗收

`V62B-UNSEEN-PAIR-01` 預留應改路、應維持各一例。公開協議的 SHA-256 為 `4783da894ad15b5bffeeeb08cd46595b91c8ca3fdae0c0955146720d7a98daf1`。公開協議和評分器已固定，私有案例、事前答案、分派承諾與本批費用額度由獨立保管者提供。本次狀態是 **NOT_RUN**，實際進場 0/2，兩個槽位各為 0/1；付費推理 0 次，供應商 token、費用與真模型決策品質未測。公開演練與歷史 25 個槽位各保留自己的分母。已曝光的 h81/h92 記為歷史對照。[機讀狀態](acceptance/v62b/study/not-run.json) · [配對方案及外部保管者執行說明](acceptance/v62b/study/README.md)

本次未比較架構效益。涉及單層對照時，A 的 V6.1.1 前置修補可固定到 `c6fc20a`；該次 `v6_native.py` 的 SHA-256 為 `1f27db22224b51ada5a39094ec1c4f5256f7ba65d942cebf349b85f2383c6057`，原生 `INSTRUCTIONS` 文字的摘要為 `23fe4d4a01d45cc18580e7b3459eb0e024f2f3c21c0a3c28af879de60d4d7f43`。B 在適用的 Run 額外提供證據指引，該文字的摘要為 `b3c08f1bd67f1cd281dba65a510ca21efcf06fac4565554c7e6c49a17ccb9b11`。真模型單層基線仍須依相應實驗的宿主回執判讀。正式封存終評屬於 V6.3。[單層修補回執](acceptance/v62a/prerequisites/receipt.json)

## 重現本地驗收

在 B checkout 內使用固定的 Python 和 Codex。以下命令執行全部內核單元測試、模組、研究輪次與配對評分規格，並重驗七種歷史協議。測試以獨立本機網路命名空間和只拒絕出站的 Clash 配置執行。四個公開研究須依上面的工程重播說明另行啟動，可取得新的逐案帳本。

```bash
trial=$(mktemp -d)
gx_clash=../../../clash/clash.new
.venv/bin/python -B acceptance/phase2a/regression/loopback.py \
  --clash "$gx_clash" --receipt "$trial/network.json" -- \
  .venv/bin/python -B acceptance/v62b/regression.py --output "$trial/regression"
.venv/bin/python -B acceptance/v62b/compat/verify.py \
  --revision 530b1380a1e3bb3cd9ccb5023d436c708512113a
(cd acceptance/v62b/receipts/engineering && sha256sum -c SHA256SUMS)
.venv/bin/python -B acceptance/v62b/study/pair.py status
```
