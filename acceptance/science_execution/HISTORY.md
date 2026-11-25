# 原分派、未結用量與後繼診斷的時間窗

歷史索引以 `f9231023b5c44c5d96ec1a974d639ea9dfe34613` 為基線，逐檔核對原始回執與該提交內的位元組。OAuth 四格的原執行候選為 `0094e35051410fcd54820362e20b275b56c9b456`。每筆來源的倉庫相對路徑與 SHA-256 均收錄在索引。[歷史索引](evidence/history.json) · [原始四格登記](../v623/oauth/receipts/admission.json)

## 原始 OAuth 四格

| 格 | 原分派 | 原 Run | 研究與帳務 | 盲評 | 原始回執 |
| --- | --- | --- | --- | --- | --- |
| 01 | P4 單層 | `gxr_01m8aca38k35b8m45w` | 研究逾時，第四個 Lead turn 的預留為 HELD | 未啟動 | [01](../v623/oauth/receipts/first-attempts/01.json) |
| 02 | K7 可委派 | 尚未建立 | 已登記，未啟動 | 未啟動 | [02](../v623/oauth/receipts/first-attempts/02.json) |
| 03 | K7 單層 | 尚未建立 | 已登記，未啟動 | 未啟動 | [03](../v623/oauth/receipts/first-attempts/03.json) |
| 04 | P4 可委派 | 尚未建立 | 已登記，未啟動 | 未啟動 | [04](../v623/oauth/receipts/first-attempts/04.json) |

系列 `GX-V62C-PUBLIC-COVERAGE-OAUTH-01` 已登記 4 格、實跑 1 格、啟動盲評 0 格。01 在事件 32 開啟分支，事件 52 准入量測節點，一個 CPU Job 成功。Lead 在事件 72 實際讀到 `common_backbone` 的具名 coverage 行，分子 149、分母 164，獨立複算一致。Lead 嘗試沿用方法時漏填 `disposition`，GX 未採納該項決策，隨後研究達到時限。兩筆成功的決策均早於讀數。[四格摘要](../v623/oauth/receipts/first-attempts/index.json) · [第一格事件與研究回執](../v623/oauth/receipts/first-attempts/01.json) · [原執行紀錄說明](../v623/oauth/README.md)

另以原生 rollout 的 SHA-256 對上公開中斷稽核。第 149 行是 Lead 的原始決策呼叫，其中的 `comparison_id` 與原資料庫事件 32 的登記值逐位元相等，`disposition` 欄位缺失。第 152 行以相同官方呼叫 ID 回覆 `SCHEMA_INVALID`，並提及既有比較。原資料庫在讀數事件 72 之後沒有已採納的決策。索引保留這兩行及比較 ID 的摘要，原始對話與參數留在原工作目錄。[去識別化欄位核對](evidence/history.json) · [原 rollout 摘要](../v623/oauth/receipts/interrupted-turn.json)

第一格前三個 turn 分別結清 82,502、64,324、98,459 token，合計 **245,285**。中斷的第四個 turn 有 12 筆可識別回應，合計 **333,311** token，之後有未配對的尾段。整格可證用量下界為 **578,596** token。末 turn 的預留仍為 HELD，整格最終用量與供應商美元金額皆為未知。若取得晚到的官方終值，操作員應對原工作目錄、原 turn 追加對帳。後繼診斷另立分派與結果。[中斷 turn 帳本、原回應覆蓋與終值核對](../v623/oauth/receipts/interrupted-turn.json)

另兩個原系列各有自己的分母。硬額度系列 `GX-V62C-PUBLIC-COVERAGE-LIVE-01` 真模型開跑 **0/4**，原預檢記錄缺少該系列授權、可驗證的逐請求硬上限及獨立裁決者。私有未見配對 `V62B-UNSEEN-PAIR-01` 的兩個條件皆未入場，維持 **0/2**。公開工程演練、本次診斷與第三沉積研究各自記錄。[硬額度原預檢](../v623/evidence/live/hard-preflight.json) · [私有配對原狀態](../v62b/study/not-run.json)

## 後繼診斷如何判定截止

每案首筆推理前，登記四個絕對 UTC Unix 時間並綁定到原 Goal 的 `research_window`。重啟讀取同一份登記。公開診斷預設開工 15 分鐘、在途收尾 6 分鐘、唯讀對帳 2 分鐘。研究判分使用開工觀察窗，尾段才達成的效果單列。[後繼診斷登記與時鐘](diagnostic.py) · [執行端時間窗](../../gx-h/gx_core/p0/research_window.py)

| 登記區間 | GX 行為 |
| --- | --- |
| `[start, new_work_until)` | 可准入新的 Lead／子模組 turn、steer 與 CPU Job。 |
| `[new_work_until, drain_until)` | 已在途的回應和已准入的 Job 可完成，已形成的科學決策仍經原交易驗證。若決策要求下一個 Job，節點可留下待辦，新的 Job 不會入場。 |
| `[drain_until, reconcile_until)` | 停止研究 Driver，對原請求作有界的唯讀對帳。已寫入的決策可按原回執重讀。 |
| `reconcile_until` 起 | 結束本次等待，缺少終值時保留已知下界、原請求 ID 與未結狀態。 |

截止作用在 GX 的模型投遞與 Job 准入。一次已准入的原生 turn 可能包含多筆實體推理請求，登記的開工截止沒有逐 HTTP 請求的硬限制。只要原預留已有穩定識別，斷線恢復先查原請求；同一決策回執可重讀。若在 GX 預留後、實際送出前跨過截止，該次未送出的預留會釋放。[模型投遞](../../gx-h/gx_core/p0/root_delivery.py) · [Job 准入](../../gx-h/gx_core/jobs/service.py)

從本候選的倉庫根目錄執行離線驗收。8 個時間窗案例與 30 個原有投遞、計量及動態 Job 案例合計 38 項通過。時間窗案例實際開新資料庫連線重啟，並用本機對端重演已送出未記下回執、逾期拒絕新 turn／steer、晚到終值只結算一次，以及終值始終缺失。CPU 案例另驗證既有程序跨截止完成、新 Job 准入交易完整回滾、尾段決策接受後留待執行，以及決策準備跨越收尾期限時無法提交。付費模型呼叫數為 0。[時間窗測試](../v624/test_research_window.py)

```bash
PYTHONPATH=gx-h:gx-h/tests:. .venv/bin/python -B -m unittest -v \
  acceptance.v624.test_research_window tests.test_p0_root_delivery \
  tests.test_p0_meter_recovery tests.test_v6_jobs
```
