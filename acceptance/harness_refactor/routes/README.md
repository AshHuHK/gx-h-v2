# 研究意圖、局部重開與 CPU 作業恢復

正式入口 `bin/gx-p0 research` 為新 Goal 固定 `gx.harness/2`。Lead 以現有 `gx_decide_revision` 提交科研動作。穩定意圖 ID、修訂、問題、可選父意圖、負責人、預期材料、喚醒條件及執行綁定都記在原有的不可變 GraphVersion。已開始的 Job 保存原始節點、圖版本、輸入與外部句柄。一般查詢可用 `gx-p0 status --state STATE --run-id RUN` 的 `research_intents` 與 `node_attempts` 相互追蹤。

| Lead 動作 | GX 保存的結果 |
|---|---|
| `open_intent` | 以 `local:` 別名開題，回執給出正式 ID。第一版可只含問題。 |
| `bind_intent` | 依目前修訂，綁定同次決策新建的一個或多個節點，或一個已委派的模組。原節點與舊修訂保留。 |
| `resolve_intent` | 引用授權範圍內的科學材料完成此意圖，允許新增零個 Job。 |
| `converge_intent` | 留下本路線的暫定收斂與目下證據。其他路線依各自的喚醒條件繼續。 |
| `reopen_intent` | 提供新證據，或此路線上次處置後送達的 PI 資訊。該意圖原地增加修訂。 |

委派可先在 `gx_open_module` 指定 `route_intent`，再以回執中的 `module_instance_id` 綁定。即使指定研究員先行交回報告，Lead 仍能關聯已完成的原始委派。模組無權修改 Run Lead 的路線。發布讀數與科學判斷依原有證據及委派範圍執行。

## 重跑

從 product-multi-workflow 根目錄執行。測試傳輸供給可重現的原生工具回調，計算使用實際的本機 CPU Job。正式模型行為由場域實驗另行記錄。

```bash
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B -m unittest -v acceptance.harness_refactor.routes.test_routes

PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a:acceptance/multi_workflow .venv/bin/python -B -m unittest -v \
  tests.test_v6_decisions tests.test_v6_jobs \
  acceptance.multi_workflow.test_advisory_rounds acceptance.multi_workflow.test_lead_graph_conflict
```

設定 `GX_ROUTE_RECEIPTS=/tmp/gx-route-receipts` 可另存本次 Run、事件、Job 與過程。原始兩項紅燈見 [baseline-red.log](baseline-red.log)，修正後五項見 [green.log](green.log)，每項有獨立的 [JSON 回執](receipts/)。

## 驗收邊界

- 重送同一次綁定只取得同一圖回執。關閉原客戶端、獨立啟動公共 CLI 的 `resume`，在外部作業已啟動、尚未寫回啟動確認時以代碼 73 強制結束該進程。第二個獨立的公共 CLI `resume` 對回同一 Job，實際 CPU 產物成功。任何二次外部啟動都會使驗收失敗。離線後續 Lead 在期限到達時仍為 READY，CLI 正常回傳 75。
- 第一條意圖引用現有材料直接解決，另一條暫定收斂。收到 PI 補充時，過時修訂遭拒，僅受影響意圖重開。研究全程零個 Job。
- 有在途 CPU Job 時，既有材料無法冒充新反證。獨立新讀數只重開目標意圖。另一條路線隨後由 Lead 明確改綁，舊 Job 仍保有原始版本、輸入與操作鍵，新綁定有自己的 Job。查舊版圖只會看到當時已綁定的節點。
- 已委派模組嘗試變更 Lead 路線會被拒。原模組報告已完成後仍能綁回恰當路線。錯路不產生圖修改。
- 明訂的喚醒條件在其他意圖的修訂後仍有效。原意圖自行結束後，該條件從後續回執中移除。

既有決策、CPU Job、固定收斂與衝突恢復共 25 項驗收見 [regressions.log](regressions.log)。初次整合 24/25 的 [regressions-first.log](regressions-first.log) 保留。預算驗收現於實際 Job 准入點確認一秒 CPU 的不足、零 Job 與零預留。


## 4096 字節的原生 Lead 交付

水文用例從正式 `research` 啟動，以兩份公開 USGS 時段資料接入已註冊的 CPU 插件。執行一個原始 Job，保存方法與決策點，再在兩份材料上實例化。一個新的 Job 實際運算，另一個節點正確復用原有輸出。兩個受控原生研究員發佈帶雙根來源的中文原話，12 個 Join 完成交付時，同一份 Lead 簡報亦含 2 個決策點與 3 則主題消息。

首次混合交付為 4166 字節，兩筆已登記的 `right_minutes` 均從首屏消失。另記錄到截短隱藏了已具備來源的 `gx_save_recipe`，以及可用的 `gx_delegate_branch`。修復令端點先於 Join 與決策點明細排入首屏，並在分配內容前計入所有區段的 JSON 標頭。[原始故障](compact-delivery-before.json) · [配方工具故障](compact-menu-red.log) · [分支工具故障](compact-branch-menu-before.json)

修正後 Run `gxr_01m8fqzvgz8bgnc1q0` 原生前綴加 UTF-8 正文共 **4052/4096 字節**。兩個產物各自的首行均帶回核驗來源的列鍵及 `4320.00 min`。同一次 Lead 任務經 `gx_read_artifact` 和 `gx_task_context` 均能取回完整中文原話、兩個原始 hint ID 與來源引用。完整及截短的授權工具集合相同，歷史 Goal 與明確禁用委派的 Goal 均有反例。此實驗使用受控原生回調、真 CPU Job 與實際投影和授權工具。[回執](compact-delivery-after.json) · [兩項驗收](compact-delivery-green.log) · [十四項既有回歸](compact-compat.log)

```bash
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a .venv/bin/python -B -m unittest -v acceptance.harness_refactor.routes.test_compact_delivery
```
