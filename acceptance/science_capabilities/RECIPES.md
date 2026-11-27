# 具名输出与科研配方

`add_node.inputs` 仍使用下游方法的输入顺序。对于来自另一个节点的输入，`input_ports` 的 `index` 从 0 开始，`output` 是上游方法声明的文件名。可以从同一个节点选择多个不同文件。未指定端口的历史图沿用原先保存的主产物。

```json
{
  "op": "add_node",
  "branch": "local:analysis",
  "node": "local:read",
  "capability": "<consumer>@1",
  "inputs": ["local:split", "local:split"],
  "input_ports": [
    {"index": 0, "output": "cohort.json"},
    {"index": 1, "output": "evidence.json"}
  ],
  "depends_on": ["local:split"],
  "parameters": {},
  "question": "哪些队列成员获得可定位的记录支持？"
}
```

GX 先核对这些文件在上游方法中有声明。派工时按完成回执中的名字与哈希取得确切产物，再由既有输入清单冻结其文件内容。缺少所选可选文件会阻断依赖。切换清单顺序不会改变下游输入。

## 在科研任务里复用

1. 当前 Goal 的 Lead 在收到成功结果后调用 `gx_save_recipe`。提供 `recipe_id`、`version`、`title`、`nodes: [{slot,node_id}]`、`inputs: [{name,artifact_ref}]`、`id_alignment`、`missing_values` 与 `units`。GX 从实际 Job 记录方法版本、内容哈希、依赖和具名输出。配方收入当前工作区目录。
2. 在新题中调用 `gx_query_catalog`，取 `kind: "recipe"`。给 `reference` 可查定义、输入类型及方法可用状态。选择的目标 Goal 需要持有配方中各方法的确切版本。
3. 调用 `gx_instantiate_recipe`，传入 `recipe`、`digest`、`branch`、本题 `inputs` 与节点名 `prefix`。将返回的 `ops` 提交给 `gx_decide_revision`。同一决定可先加入开分支操作。若根据新问题替换其中一个方法，实际派工采用修改后图中的绑定，应报告该实验使用了变更配方。

同一工作区的后继 Goal 可用全新的目标和原件。配方定义保留方法、图结构与科学约定，历史输入和旧 Job 输出留在来源 Run。带执行条件的节点及缺少上游依赖的子图需保持原图执行。不同工作区各自维护本地目录。

## 复验

从发布目录运行以下命令。第一条执行协议、具名绑定、重放与旧 Job 恢复测试。第二条使用受控的原生工具回调与真实 CPU Job 完成两次公开结构量测。

```bash
(cd gx-h && python -m unittest -q tests.test_v6_named_ports tests.test_v6_jobs tests.test_v61_native_contract)
python acceptance/science_capabilities/recipe_reuse.py --receipt /tmp/gx-structure-recipe.json
```

首次验收先用 1QYS 建立 `deposited-backbone-coverage@1`，再由仅带 6BEN 原件的新题从目录查得该配方。两次均从 `qc.json` 读取具名端点并提交结题决定。1QYS 的完整骨架为 92/106，比较应限于建模位置。6BEN 的模型 1 为 8/8。该计数判据用于评价坐标是否齐备，构象准确性仍需另外评价。两次方法、原件、Job、产物、配方摘要及结题决定均可在回执中复查。[回执](evidence/recipe_reuse.json)


## 预报、证据决策与校准

保存含有 `model_prediction` 的已完成计算会生成 `gx.research-recipe/2`。每个具名输出记录 Job 当时绑定的性质和输入角色。先读预测产物再以 artifact 引用派评分 Job 时，选入两步即可恢复它们之间的具名数据依赖。复用时绑定当前 Goal 的材料。

`gx_save_recipe` 的可选 `decision_point` 接受 `step`、`output`、`question`、`actions` 和 `stop_condition`。保存开放问题与可选行动。当前 Goal 的节点结算时，GX 唤醒新的 Run Lead，交付该节点与 Job 的身份、实产输出、摘要及性质。Lead 用当前读数决定是否维持、更改路线、补测或停止。首次决策及其原始依据留在原 Run。压缩简报保留完整简报引用。

可选 `calibration` 指定使用原始材料的独立准备步骤和适用范围。目标 Goal 的 `calibration_scope`、原件内容、方法、有效参数、环境与已固定的小型方法资源一致时，实例化回执引用原校准 Job 及其完成摘要。正式数据流仍需校准产物时保留该步骤。条件变化、输入转换器、权重或未能识别的外部数据会令校准重新执行。使用 `calibration_parameters` 改变条件，或指定 `independent_repeat: true` 独立复做。独立重复的 Job 身份包含节点 ID，原节点崩溃恢复继续办理同一 Job。

[记录覆盖与校准回执](../science_plugin_blueprint/recipes/coverage.json) · [条件变更后重算](../science_plugin_blueprint/recipes/recalibration.json) · [预报、重复计算与冷恢复](../science_plugin_blueprint/recipes/sensor-prediction.json)
