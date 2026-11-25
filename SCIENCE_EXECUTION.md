# GX-H 科研執行交付

以已發布的 `f9231023` 為基線。新的 Run 使用 `gx.evidence/6.2.4`。研究問題是獨立採集的新沉積能否重現「三對鏈的中位數掩蓋個別鏈對的區段差異」。

| 可驗收的工作 | 原始紀錄與重跑入口 |
| --- | --- |
| 決策、來源與生效 | Lead 在 `gx_decide_revision` 提交理由、實際操作，可選已收到的具名數值列。GX 推導唯一比較、引用和喚醒。歧義時回傳具體欄位。見[產品回歸](acceptance/science_execution/PRODUCT.md)與[官方宿主實收欄位](acceptance/science_execution/evidence/official-decision-wire.json)。 |
| 材料時序 | 先量親本 164/164 後，P4 與 K7 各自保持未量。兩個 CPU Job 實測 149/164、54/164。等價重匯保留原時序，無法核實的變換留下未決原因。[事件與重跑命令](acceptance/science_execution/evidence/chronology.json) |
| 原四格及修正後的公開兩案 | 原 P4 的事件 72 已送達 149/164。原生呼叫使用正確比較 ID，漏填處置，GX 拒收，觀察窗內沒有生效的讀後動作。四格中的其他三格未開跑。舊格至少用掉 578,596 token，整格終值與美元金額未知。[歷史回執](acceptance/science_execution/HISTORY.md) · [兩案啟停、對帳與覆核](acceptance/science_execution/DIAGNOSTIC.md) |
| 第三份沉積 | 122 個公開候選中，7 份符合人類 TL1A 身分。扣除兩份歷史參照，5 份都只有一條實際建模的靶標鏈，合格新沉積為 0。原題維持待驗。日後取得獨立採集、位置可映射且有三個可比較實建副本的原件，才可驗證該現象。[逐目標表與來源](acceptance/v624/custody/README.md) |
| 逐列數值與獨立複算 | 映射版 `deposited-block-influence@2` 交付選中區段三對鏈與中位數的前、後、差值及有效位置數。對兩份公開歷史控制，獨立 SVD 複算與產品差值最大相差約 5.7×10⁻¹⁴°、7.8×10⁻¹⁶ Å，均在原容差內。[數值與單位](acceptance/science_execution/evidence/method-summary.json) |

**目前可採的質控建議。** 在這兩份已有三個實建副本的歷史原件上，逐鏈對列出指定區段的改變和中位數。單鏈新候選無法提供此比較所需的三個鏈對，該欄記「資料不足」。採樣範圍、遮罩及適用區段跟隨各自原件。[歷史數值報告](acceptance/v623/V623_SCIENCE_REPORT.md)

## 執行

在鎖定環境的候選 checkout 根目錄執行。部署位置及截至交付時的原始候選 SHA、Run、測試與凍結狀態見本頁下方的驗收紀錄。OAuth 受現行 Clash 路由約束。下列預檢只讀取宿主和訂閱資訊。

```bash
.venv/bin/python -B acceptance/science_execution/diagnostic.py preflight --host oauth
.venv/bin/python -B acceptance/v624/custody/verify.py --work ../science-execution-validation/custody
```

對已查證的後繼授權，操作員可使用下列登記與首次開跑命令。原始四格不受新登記影響。兩案的審閱、恢復、停機及匯出命令列於[操作步驟](acceptance/science_execution/DIAGNOSTIC.md)。

```bash
GX_DIAG=../science-execution-validation/oauth-diagnostic
GX_STUDY=../science-execution-validation/oauth-study
GX_ORIGINAL=../v623-validation/final-v4x8yihr/oauth-live
GX_APPROVAL=$(.venv/bin/python -B acceptance/science_execution/approval.py \
  --diagnostic-work "$GX_DIAG" --science-work "$GX_STUDY" --old-work "$GX_ORIGINAL")
.venv/bin/python -B acceptance/science_execution/diagnostic.py register --host oauth --work "$GX_DIAG" --approval "$GX_APPROVAL"
.venv/bin/python -B acceptance/science_execution/diagnostic.py run --host oauth --slot 01 --work "$GX_DIAG" --approval "$GX_APPROVAL"
```

**遷移與回退。** 停止寫入、為原狀態與產物建立一致備份，在相同路徑切換實作後恢復原 Run。已用舊版建立的研究實測升級、兩次冷啟動，再換回舊版與升級前備份。3 筆原決策、1 個 Job 和 4 個 turn 的業務內容與原始請求雜湊都相同。新研究使用獨立狀態目錄，回退時保留其資料。[遷移紀錄](acceptance/science_execution/evidence/v623-cold-migration.json)

## 已執行的候選與驗收

部署位置為同層 `../science-execution-deployed`，乾淨候選 `b60a76c95d45782379e5c4590ee867bd23346cee`，產品提交 `46cf0b2`。在該處執行上列診斷與授權命令，即可使用同一個工作版本。啟用的宿主為官方 Codex `gpt-6.2-astra/max`，帳戶與服務額度預檢成功，該預檢發出 0 筆推理。[預檢回執](acceptance/science_execution/evidence/subscription-preflight.json)

| 固定候選的原始分派 | 模型來源、已生效行動與成本 |
| --- | --- |
| P4 單層 `gxr_01m8ahef005364jxw5` | 官方宿主＋腳本。事件 71 收到 149/164，79 首次提交獲採納，90 對照 Job 成功。讀數到實際效果 1.456 秒。13 次本機請求、455 模擬 token、0 筆模型推理。 |
| K7 指定委派 `gxr_01m8aheyjmqqterx5r` | 官方宿主＋腳本。子會話的 CPU 量測為 54/164，報告在事件 96 發布、97 釋出，Lead 在 105 收到，114 引用讀數並撤回分支。耗時 0.787 秒。19 次本機請求、665 模擬 token，其中 Lead 490、模組 175，0 筆模型推理。 |
| 合成第三樣本 `gxr_01m8ahefw7typ6ny1x` | 官方宿主＋腳本。29 接納事前承諾，33、36 開封兩個關聯輸入，49 建立 Job，54 發布數表，75 引用並結題。1 個實體 CPU Job、2 次產物讀取、6 筆本機請求、210 模擬 token。原件獨立複算最大差為 2.66×10⁻¹⁴ Å。 |
| 真實第三樣本 | `NOT_ENROLLED / NO_ELIGIBLE`，0 次新端點計算、0 次模型推理。兩份既有參照的獨立數值複算另列。 |

兩個診斷的首次取數、決策、執行圖效果、原始回執及修復後的冷恢復均已匯出。合成研究的三個鏈對和中位數、各自前後值、資料適用域與獨立誤差均可逐列重查。[兩案與崩潰恢復](acceptance/science_execution/evidence/diagnostic.json) · [第三樣本完整工程回執](acceptance/science_execution/evidence/synthetic-study.json) · [真材料登記](acceptance/science_execution/evidence/third-study-registration.json) · [盲測操作與正式研究命令](acceptance/science_execution/study/README.md)

**真模型與凍結。** 本次新的 OAuth 兩案均為未開跑，真模型行動簽收 **0/2**。現有帳戶已通過官方預檢，已查到的 PI 授權涵蓋舊四格。新系列需要同一帳戶的後繼授權，涵蓋原未結曝險與這兩個指定分派。V6.2 收尾的凍結條件是兩個原始真模型分派各有已生效動作、用量終值及理由覆核。第三份沉積另需符合材料條件。目前 V6.3 與 V7 的後續退出條件仍待驗。

固定候選上重跑三條基線命令，原四個 Run 的 24 份公開資料包、原研究的 7 次決策及 4 個數值產物均可重建。測試與原始輸出摘要見[驗收清單](acceptance/science_execution/evidence/validation.json)。首次完整舊版測試有一項用舊政策開新測試 Run，已將該歷史測試明確標記為 replay，再跑全組。
