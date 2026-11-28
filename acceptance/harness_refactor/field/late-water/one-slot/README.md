# 冷恢復後同一研究員收到、讀完第二站，正式公開報告未獲接納

正式 Run `gxr_01m8fthqsy7h6t5z5x` 使用 `48dfdb04d3ef46bfd70b3f88ec78828f411e6fbb`。290 個已部署 Python 檔與該 Git tree 完全相符。這次驗證兩項交付修補，沒有部署後來合併的其他 close 修補。題目、唯一的定向 PI 附件、資料來源、插件與科學評分延用首次預登記。Lead 自主選擇兩名相互隔離的研究員，每席配置 1 個 worker task、5 個 API call、400,000 tokens。

| 核對點 | 原始證據 |
|---|---|
| 早期判斷 | 事件 61 在晚件投遞前採納首站連降、首末降 13.53% 的暫定判斷，並以 `open_intent` 保留跨站比較。Lead 事先約定兩站降幅差達 5 個百分點才稱「明顯較大」。 |
| 斷線與新材料 | 事件 67 後 SIGKILL 退出 -9。原 Run 唯一離線定向訊息的第二站 SHA 為 `d6720b5b…`。事件 72 撤換原資料員短任務，74 保存 PI 原話。78 以同一席延續工作，保留原 task 與 grant 72 的關聯。另一席原定量研究員保持其任務並完成 102。 |
| 官方輸入與原件實讀 | 指定資料員首個有效恢復 turn `01a21fab-3a3c-7422-96a3-5e009548a817` 的凍結簡報有晚件 ref、feedback_id 與 PI 原話，官方 ACK 核對正文 SHA 成功。研究員也在 `gx_task_context` 收到同一訊息；原生調用 `exec-5fc21672-8304-4079-b4ff-d2b3af63fbda` 確實讀完 **3,400 bytes**，與 USGS 附件 SHA 逐字相符。後續回傳 0 列的 endpoint 分頁獨立計列。 |
| 研究員有否改變行動 | 指定資料員先讀首站。收件後轉查第二站、核查兩站原件，算出 Little Falls 降 13.53%、Northwest Branch Anacostia 降 57.47%，相差 43.94 個百分點。逐字對上獨立盲底。它前後兩次目錄查詢都是 `daily discharge`，可計入改變查證對象，未觀察到改換目錄查詢詞。 |
| 報告與下游 | 官方 wire 的 `call_6daf0eb139f64d788d1a3a386033d37c` 等五次雙站報告使用真實第二站原引用，均遭 `SCOPE_DENIED`。GX 的公開報告會一併釋出所引原件；PI 此次僅將檔案定向給該席。錯誤回覆讓研究員再讀早已讀過的原件。最後 140 是 GX 因該席 token allowance exhausted 產生的 PARTIAL，沒有雙站數值。狀態記為 DELIVERED，無研究員科學採信回執。 |
| 仍未證明 | 兩席都由 Lead 選作 isolated。未形成兩席交換原話與接力行動，Lead 也沒有收到可引用的雙站正式報告。這次無 CPU Job，未測外部 Job 接管。 |

從產品根目錄可重算獨立資料與核對官方輸入，原始 wire 拒絕、正式報告與約束原因在同一份 JSON 中分欄保留。

```sh
.venv/bin/python -B acceptance/harness_refactor/field/late-water/check_repair.py acceptance/harness_refactor/field/late-water/one-slot/receipt.json
python -B acceptance/harness_refactor/field/late-water/verify_reference.py
```

[獨立工程復驗預登記](preregistered.json) · [正式回執](receipt.json) · [完整原始參照](../reference.json)

## Lead 的實際後續行動與終點

在只收到 140 的部分報告後，Lead 另派一名可共享結果的「Archive source reviewer」（原生調用序號 70，`exec-5443a027-a307-4afb-9fcd-d65df8f7d822`），要求它檢查自己能讀到的投遞、核對站號、流量及來源。新席位在 `gx_task_context` 只見首站，實讀該原件與 140 報告。它沒有取得合法可讀的第二站獨立來源，於事件 190 說明缺口，以官方研究員身分提交 PARTIAL，唯一原引用是首站。原定量席位在 102 提交的首次數值審查得到保留。

Lead 的判斷 178 和 209 均已生效。209 引用 102、140、190 的正式報告，維持首站降 13.53% 的判斷。事件 210 建立待答問題 `piq_9394da4d806bc5ea5c42bbc25554c36b`，請 PI 將第二站原檔交給 Lead 以完成比較。至事件 212，Run 仍 OPEN，0 個可執行科研任務，所有官方科研回合均已結束，全案共 0 個 Job。原始指定資料員讀完第二站且算對答案，與 Lead 實際發出了什麼結論可在回執中分別核查。

若 PI 日後決定共享，可由 `gx-p0 message` 針對上列問題在同 Run 提供新、研究層級可讀的原件。原試驗沒有這次追加投遞。外層 `resume --timeout 1200` 的等待期限與科研已達 `pi_answer` 是不同時間點。

[機器核對結果](result.json) · [最終工作狀態](end-state.json)
