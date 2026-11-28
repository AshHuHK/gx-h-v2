# A′ 收尾時的審閱引用與租約交接

原 Run `gxr_01m8g22y65r8zxbfqw` 的五份審閱都出自同一個 Lead，也都有該席的原生收件回執。第一筆關題漏引這五份。GX 在事件 604 收回租約，605 保存 `STALE_REVISION`。儲存的回執建議重試 `gx_run_status`。官方 rollout 顯示模型實際收到的精簡工具回應只說下一席可以引用「listed review refs」，連 ID 都未送出。原席後來補齊五份，又在事件 612 收到 `SCOPE_DENIED`。兩層原回執及事件前綴收錄在 `field-before.json`。

修補 `9ab2098` 適用於 `gx.harness/3`。GX 能證明所有晚到事件都是當前席位已收到的無特定主張審閱、期間沒有 Job 變動時，會傳回可更正的 `SCHEMA_INVALID`，附上缺少的原件 ID。當前席位可以在同一個任務、租約、回合提交修正。若 PI 意見等外部變更已使原前提過時，GX 仍按現有交易交棒。已退役席位的工具回應與持久化回執會記載實際交接事件與不可重試的處置。

| 回執 | 驗證範圍 |
|---|---|
| `field-before.json`、`field-after.json` | 以唯讀 SQLite 及官方 session 檔取得 A′ 原始請求及拒收，把事件截在首次撤權之前重放關題新鮮度檢查。原版對五筆缺引給出過時錯誤，候選明列五個精確 ID 並保留原席的更正權；當時的第二筆實際請求已補齊五份。 |
| `before.log`、`after.log`、`same-turn.json` | 普通 research CLI，受控模型、真實原文、查閱、審閱和工具回調。兩份審閱漏掉一份，只提示該份；同席補齊後關題。重送原 call ID 取回原回應。 |
| `late-pi.json` | 第一次可更正拒收之後有外部 PI 原話送達。真實交接恰有一次，失去租約的席位收到不可重試回應。後繼 Lead 收到 PI 原話及兩份審閱並結題，新增 Job 為零。 |
| `affected.log` | 13 個既有的來源作用域、跨回合身分、私域、過時收尾和 PI 變更案例全過。 |

兩個新案例在 `e8140c6` 都出現預期失敗，在候選均通過。本次不變更正式 A′、既有分數或科學產物，沒有啟動替代研究 Run。

```sh
PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a GX_CODEX_OFFLINE=1 python -B -m unittest -v \
  acceptance.harness_final.close_corrections.test_review_retry test_source_decision_receipts \
  acceptance.harness_final.late_pi.test_late_feedback
PYTHONPATH=gx-h python -B acceptance/harness_final/close_corrections/replay_aprime.py <archived-aprime-arm>
```
