# 同一科研 Goal 切换方法

Lead 和受委派研究者可以通过 `gx_query_catalog` 查 `method`、`module`、`research`、`database`、`skill` 与 `recipe`。先按问题搜索，再按版本引用读详情。研究者的可执行方法限于其委派与当前 Goal 的交集。已装方法的 `available` 表示当前授权可选，实际运行回执由 Job 提供。既有体系中仅定位到的说明显示 `reference_only`。可重放的 recipe 来自实际运行后保存的子图。

PI 可以在普通 Goal 配置中通过 `capability_options` 给出可选方法。每项含 `method`、可选的 `provider` 和方法的可移植 SHA-256 `digest`。例如以下配置允许在同一 Goal 中选择已发布的确定性测试方法。

```json
{"capability_options":[{"method":"fixture-grid@1","digest":"b69193d4546b42225261484872718eb475478c5836377de6cacd6b8c38b9f12f"}]}
```

Lead 用 `gx_select_capability` 提交 `method`、`action`（`enable` 或 `disable`）和简报上的 `revision`。本地操作员可从下列正常入口查看和修订。新请求用新请求 ID，回执丢失时用原请求及相同 ID 重取。

```sh
bin/gx-p0 capabilities --state "$gx_state" --run-id "$gx_run"
bin/gx-p0 capabilities --state "$gx_state" --run-id "$gx_run" \
  --enable fixture-grid@1 --revision 0 --request-id add-grid \
  --sha256 b69193d4546b42225261484872718eb475478c5836377de6cacd6b8c38b9f12f
bin/gx-p0 capabilities --state "$gx_state" --run-id "$gx_run" \
  --disable fixture-grid@1 --revision 1 --request-id stop-grid
```

新简报会显示版本及最近的启用或禁用。首次修订前的视图是版本 0。每次有效选择追加一个事件，并在同一个 SQLite 事务更新有效方法；科学 Goal 版本独立。若两个选择从同一版本提交，一个可以生效，另一个拿到当前版本再选择。Job 在同一写边界完成准入。禁用会把尚未准入的对应节点记录为 `BLOCKED`，并把具体方法与原因交给判断者；已有 Job 继续使用其原始身份、输入及方法。重新启用允许新的节点，旧结论与旧决策可以按原回执读取。

## 新研究的可選方法集合

普通 `bin/gx-p0 research` 接受含 `capability_collections` 的 Goal 配置。例如，這份配置讓 Lead 查看並選用公開來源與本部署的 CPU 分析方法。

```json
{"capability_collections":["public_sources","cpu_analysis"]}
```

| 集合 | 可選用途 |
| --- | --- |
| `public_sources` | 公開論文檢索、原文取得與可用的文獻翻頁方法 |
| `public_databases` | 已安裝的公開資料庫讀取方法 |
| `cpu_analysis` | 已安裝且宣告使用 CPU 的資料分析方法 |
| `archived_structure` | 沉積結構的量測與已安裝的歷史對照校準 |

新研究省略 `capabilities`、`capability_options` 及 `capability_collections` 時預設選取 `public_sources`。其中任何一項顯式設為空陣列，即依提供的配置建案。其他顯式項目各自生效。GX 在開案時將集合展開，固定其中每項方法的版本、方法摘要、提供方版本和套件摘要。只有 Lead 實際啟用的方法才進入執行視圖。相同 Run 恢復時沿用凍結範圍，既有 Run 可用原命令繼續。

插件作者可以在 `gx_plugin.json` 的 `labels` 中加入 `"gx_capability_collections":"cpu_analysis"`。多個集合名稱用半形逗號分隔。套件中的每個方法都會依標籤登記；需要不同範圍時應各自打包。部署者也能在 `config/workflow_packages.json` 的 `research_method_collections`，以集合、精確提供方與方法版本對照既有套件。新 Goal 只從已安裝並已核對的套件展開授權。兩個提供方登記相同方法版本時，PI 可在 `capability_options` 指定該方法的提供方與摘要。

有目前發現策略的父 Run 建立後續研究時，子 Run 繼承原有的可選版本。顯式提供 `parent_method_options` 可將範圍改為從父 Run 生效方法中選定的清單；空清單保留空範圍。每個新題仍由 Lead 查詢目錄並決定是否採用。

## 验收

从产品根目录运行以下命令。用新的输出路径可保留首次回执。

```sh
python acceptance/science_capabilities/capability_views.py --output /tmp/gx-capability-views.json
```

五项受控验收使用真实 CLI、SQLite、原生科研工具回调及一个本地 CPU Job。覆盖并发选择、图准备与采纳之间改版、读数回执重取、方法摘要不符、操作员禁用、下一轮简报、研究者提交部分报告，以及已准入 Job 的冷恢复。科研回合由脚本驱动。新增测试最初复现了缺少同题选择入口，角色测试另发现了变更后跨版本重用旧目录读取的问题。[受控回执](evidence/capability-views.json)
