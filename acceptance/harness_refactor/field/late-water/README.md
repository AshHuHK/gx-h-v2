# 第二站遲到與斷線復原的正式水文試驗

首次正式 Run `gxr_01m8fs36kqvx147ysh` 已保留。Lead 與兩位研究員能依首站原件給出正確的暫定判斷。第二站在 GX 停機時成功排入，重啟後也正確指派給原資料員。但這位研究員恢復時收到的仍是第一份簡報，於是對已到的第二站仍報「待到」。Lead 隨後正式詢問 PI 共享原件，Run 停在待答。這次試驗發現一個影響研究結論的交付缺陷。

| 證據 | 首次結果 |
|---|---|
| 科學題與帳戶 | 2023 年 4 月 1 至 3 日兩站的流量退降。首次只給 Little Falls 的三日日平均原件。gpt-6.2-astra，max，官方 Codex 0.169.0，現有 OAuth。題面未指定方法或答案。 |
| 早期判斷 | 定量研究員於事件 79 出具報告。其原話記下 Little Falls 首末降 1,350 ft³/s、13.53%，並明確保留第二站與成因缺口。Lead 在斷線之前兩筆暫定裁決因格式被拒；首次正式採納發生於復原後事件 106。 |
| 正常離線投遞 | 在事件 82 後對本試驗 Driver 發 SIGKILL，退出 -9。相同 Run 的 `gx-p0 message --queue-only` 只排入一次，事件 84 登記附件。新 PID 的 `resume` 於事件 87 授權原資料員、88 登記 feedback。 |
| 真模型看到的內容 | 原資料員 `module_e5c2d6f801a9f79f65d9078a64896349` 的恢復 turn `01a21f94-dd7c-7373-8a3b-b48aa807ffd2` 仍收到 event cursor 21 的簡報，僅含首站。事件 116 的原報告只引第一站，並稱第二站資料待到。第二站完整原件與來源段落的讀取皆為 0。 |
| 首次研究結果 | Lead 的事件 149 實際引用 79、116 兩份報告維持首站結論，指出第二站只顯示 private-material notice。事件 150 請 PI 分享已投遞檔，停於 `pi_answer`；`feedback_status=PENDING`，實際收件清單空。保留原來的資料作用範圍。 |
| 橫向協作與改搜 | Lead 自主設定兩席皆為 `isolated`。兩份獨立報告均已送給 Lead；無研究員互傳原話、無收件人據晚件改搜、無有版本的局部重開。學術上保持未知合理，但這條橫向接力未成立。 |
| 無關工作與恢復範圍 | 先前完成的獨立定量報告仍為同一份 79。研究員直接審閱原始 JSON，整次 0 個 CPU Job，故無外部 Job 接管實證。 |
| 獨立科學參照 | 原檔逐筆核為已批准的日平均流量。Little Falls 9,980 → 9,230 → 8,630 ft³/s，首末降 13.527054%；Northwest Branch Anacostia 60.9 → 40.7 → 25.9，降 57.471264%，多 43.944210 個百分點。這是由保管者單獨計算的盲底，未傳給任何模型，首次 Lead 並未完成兩站比較。 |
| 部署邊界 | 本次固定 `gx_core_sha256=d49ca205409687a58938156de3a24cbc66b83ca62fb76f688ccdc47223807a78`。與 `5904a4f` 點對點比對 17 份路線、訂閱、派工及簡報檔；11 份相同、6 份不同。首輪開始時唯讀觀察腳本誤用不存在的資料表欄位，第一次 Driver 提早離開。已修正觀察器並使用同一 Run 復原；這次非預定斷線和稍後正式 -9 均留在回執。 |

## 核驗

在產品目錄執行以下指令。第一個重算原始檔與雙站參照，第二個檢查實際正式回執。獨立私有工作目錄仍在時，第三個重新匯出同 Run 的原始事件、完整研究員報告、官方回合、讀檔與裁決。

```sh
python -B acceptance/harness_refactor/field/late-water/verify_reference.py
python -B acceptance/harness_refactor/field/late-water/check_receipt.py
.venv/bin/python -B acceptance/harness_refactor/field/late-water/export.py ../harness-refactor-live/late-water
```

[題面](question.md) · [預登記](preregistered.json) · [首輪正式回執](receipt.json) · [獨立數值參照](reference.json) · [部署逐檔對照](deployment-comparison.json)
