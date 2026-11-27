# 正常研究決策的欄位診斷

酵母沉積紀錄比較 Run `gxr_01m8fdw087fqx283nq` 的原生 Lead 曾收到相同根路徑的錯誤，於兩層操作之間反覆移動 `stop_condition`。運行原件可由 `../gx-science/asset-study-live/yeast/state/gx.db` 的 `p0_root_calls`、`p0_v6_native_calls`、`events` 交叉核對。當次 Lead 自行修正，首次生效的決策事件是 #128，第十次提交的修正回執為 #129。

| 原始事件 | 欄位所在層級 | 修正後將提供的診斷 |
|---|---|---|
| 117、119 | 開啟研究分支 | `open_branch at $.ops[0].stop_condition` 缺少停止條件。 |
| 118 | 第二個操作是新增節點 | `add_node at $.ops[1].stop_condition` 不接受該欄位。 |
| 121 | 分支預算 | `open_branch at $.ops[0].budget_limits.wall_hours` 不在允許的單位之內，回傳實際合法單位。當次提交中的 `worker_tasks` 與 `cpu_seconds` 均為現有合法單位。 |

[原生拒收與唯讀重播](field.json) 記錄原提交識別、內容摘要、舊回執及使用原 Run 合約的新版診斷。四筆重播皆維持 `SCHEMA_INVALID` 與 `side_effect_status=NONE`。當次 Lead 沒有收到後來才修改的提示。離線重播不計模型回合。

[首次回歸](first-failure.log) 來自兩次普通 Goal 的受控原生回呼，皆重現舊提示。修正後從相同研究入口送出缺少欄位、放錯層級和未知預算單位，並檢查圖版本、Job 數與公開拒收事件。有效操作隨後能提交成功。未知的私有欄位僅在發給 Lead 的原始回執中顯示。定向回歸也捕捉到舊決策選單的兩個雜湊已漂移，在未套用這次修改的 `4504fc8` 上獨立重現。舊政策現會還原其原始 `add_node` 合約。[舊版故障](preexisting-wire.log) · [28 項回歸](regressions.log)

在產品目錄執行：

```bash
PYTHONPATH=gx-h:.:gx-h/tests .venv/bin/python -B -m unittest \
  acceptance.science_assets.diagnostics.test_decision_paths \
  acceptance.v624.test_cheap_job acceptance.v624.test_native_decision_wire \
  acceptance.v623.test_policy_compat test_v6_decisions test_v61_schema

PYTHONPATH=gx-h:. .venv/bin/python -B -m acceptance.science_assets.diagnostics.replay \
  ../gx-science/asset-study-live/yeast/state/gx.db gxr_01m8fdw087fqx283nq \
  /tmp/new-decision-path-replay.json
```
