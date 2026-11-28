# 研究者主題交接驗收

新 Goal 的子研究員沿用 `gx_send_research_hint`。直接指定收件人的呼叫仍可用。研究員也可用 `action=subscribe` 訂閱自己選的主題，用 `publish` 分欄登記問題、解釋、原始產物，或附父消息進行綜合。原生回執會留下作者、turn、原發 ID、各父消息及引用原件的摘要。有既存來源索引時也會顯示文獻識別碼，有產物 Job 身分時會顯示 Job ID。獨立撰寫的人數、不同 Job 數及獨立樣本數各有不同的科學含義，採樣獨立性需核對原件。

`forward` 延續同根，簡報將轉發者歸到原消息。`correct` 和 `retract` 追加版本，`history` 以本主題某一條消息的 `parent_hint_ids` 查其歷次版本。只呈現呼叫者目前可讀的內容。若公開解釋獲得一筆 PI 限定材料後需要修正，原讀者看到舊版撤回，有權者可讀新版。隔離盲評沒有同儕工具、主題清單或收件內容。

訂閱、原生回執與消費游標沿用同一 GX 寫入交易。首訂保存 Run 事件高水位 W，先回放至 W，再消費其後增量。每頁最多 16 則，`has_more` 表示可以續讀。在途 Root 收到聚合的提示後透過新的 `gx_task_context` 回呼讀取，原呼叫重送則重取相同回執並重新核對可見性。Lead 的 `gx_task_context` 可按 `research_topic`、`before_topic_event` 查閱較早的有效解釋。舊 Run 保留原工具契約。

## 重現

從完整產品根目錄執行。

```sh
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B -m unittest -v \
  acceptance.harness_refactor.topics.test_topic_handoff \
  tests.test_researcher_handoff tests.test_v62_module_funding \
  tests.test_v62_module_catalog acceptance.multiturn.test_pi_materials
```

[基線](baseline-red.log)記錄舊契約下四項回歸失敗。[測試輸出](verified.log)包含主題控制、定向交接、經費、舊模組目錄與 PI 材料相容案例。[CPU 交接回執](job-wake.json)取自普通 `bin/gx-p0 research` 入口。存檔核查 Job 已獲准並處於 RUNNING 時，研究員收到另一模組原話、取得同模組後繼決策任務，第二次讀取為空，同一 Job 最後產出可核對結果。

這組測試以可控原生回呼重現訊息時序，CPU Job 使用實際本地執行。真模型自行選擇改搜或維持的證據須查現場 Run。
