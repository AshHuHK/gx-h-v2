# Job 准入、額度競爭與冷恢復

普通 `bin/gx-p0 research` 建立研究。受控的本機 Codex transport 送出 Lead 委派與模組 Graph 決定。兩個獨立 SQLite 連線在可用額度皆為一的同一時點，為兩份不同的本機文字原件呼叫實際 JobService。插件各自需要一個 Job 名額。測試實際啟動 CPU 程序，注入啟動後的回執丟失與外部狀態暫時無法讀取，再改由新連線對同一 idempotency key 重送。Run、Job、付款保留與結果識別見 [job-receipt.json](job-receipt.json)。

第一個回歸重現原始故障。模組在提出兩個候選節點時就遭拒，兩個消耗點都未到達。候選實作讓新 Goal 先保存 Graph，每個 Job 到派工時才在同一筆 writer 下取得額度與身分。原有 Goal 沿用原政策。試探預算改為讀取快照，實際准入仍在寫入 Job、Attempt 與預留的交易內重新核對，未知提交繼續占用名額。私有模組與 Lead 均能在各自的下一個回合看見缺額，Lead 的回執包含已預留額度與保留識別。科學選擇留給收到缺額的 Codex。[首次失敗](before.log) · [聚焦複驗](after.log) · [相容複驗](compat.log)

以產品根目錄為工作目錄可重跑聚焦驗收。

```bash
PYTHONPATH=gx-h:. python -m unittest -v \
  acceptance.harness_refactor.budget.test_resources \
  acceptance.harness_refactor.budget.test_admission \
  acceptance.multi_workflow.test_advisory_rounds.AdvisoryRounds.test_measurement_outside_branch_resources_creates_no_job
```

能力 Manifest 可選填正整數 `resources.paid_api_calls`，表示這個受信 adapter 一次啟動占用的固定對外呼叫名額。GX 在同一交易預留獨立的 `paid_api_calls` 資源；確認未啟動時釋放，已啟動或終止時仍無法排除使用時，依聲明的名額保守入帳。供應商金額與實際 HTTP 次數需供應商自己的回執。接入真正付費方法時，該方法需遵守其呼叫數聲明，並對遠端未決工作保留可查詢的識別。

新付費方法要有站點 profile 中的數量上限、開啟的 `paid_external_services` 及 PI 的工作區／Run 額度，委派時也要分配相應資源。站點允許使用付費服務仍可讓普通研究保有零付費名額。既有 Codex OAuth 的 `api_calls` 一直單獨記帳。本站付費服務維持原配置，測試使用受控部署設定與本機 CPU，未聯絡付費供應商。GPU 驗證覆蓋資源與額度換算；本項實驗沒有提交硬體 GPU 作業。
