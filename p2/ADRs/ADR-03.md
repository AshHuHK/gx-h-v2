# ADR-03　人类 Goal、计算裁决与科学签署

**状态为提议，待审阅。** Human/PI 决定研究范围、Goal 验收条件和硬预算。Evaluator 报告测量，GX 计算机械验收结果。超出机械标准的科学主张由 PI 对具体证据签署。

## Existing · 沿用的所有者

Intent 已保存人的原始输入、版本、预览摘要和后续确认。Budget 保存额度、预留和实际结算。Artifact 可重算文件摘要，Job manifest 可核对原始输入、成功的 Job 和输出归属。沿用 `eval_specs`、`evaluations`、`evidence` 和原始审阅记录。旧 grid 对有限样本产生过支持和反驳两个方向的结果。旧 `science_decisions` 保存每轮科研任务的决定，Codex thread goal 控制工作线程生命周期。[Intent 记录](../../gx-h/gx_core/p0/intent.sql#L6) · [评估表](../../gx-h/contracts/db_schema.sql#L455) · [Job 证据校验](../../gx-h/gx_core/p0/manifest.py#L231) · [grid 回执](../../acceptance/v31/final-public-deterministic.json#L145) · [科研决定](../../gx-h/gx_core/p0/science_inputs.py#L120) · [线程 goal](../../gx-h/gx_core/p0/roots.py#L815)

## Extend · Goal 与预算授权

在现有 Intent 版本表增加 P2 Goal Contract 类型。机械层记录适用范围、指标、单位、比较符、阈值、多个指标的组合规则、允许的测量类别与来源、Evaluator 家族及已批准的精确生产者摘要、预算引用与截止时间。科学层记录要回答的主张、代理指标的解释限度、所需正交证据或湿实验。GraphVersion 固定原始 Run、Goal 条目、版本与摘要。同项目复用时核对原授权的适用范围。历史工作区共享 objective 和资源限制，evaluation_spec 由各 Run 管理。[版本与依赖](../../gx-h/gx_core/p0/intent.sql#L29) · [项目继承范围](../../gx-h/gx_core/p0/workspace.py#L12)

GX 在调用插件前保存规范化的人类输入。现有启动路径会合并 hook 的同名条目，修改路径会把同一份可变 edits 先交给 hook 再生成 diff。P2 向 hook 提供独立副本，由 GX 从保存的输入生成受保护字段。在初建、amendment 和恢复时核对 Goal pin。方法参数只能影响声明的方法步骤，例如求解器收敛容差。参数或别名若意图改写验收阈值则拒绝整项提交。放宽目标、改变范围、提高硬上限、更换 Goal 指标的生产者，即便在同一家族内，也需在已有人工入口展示具体差异并取得绑定原版本和预览摘要的确认。GX 在写入事务复核版本；并发修改后旧确认失效。[启动合并](../../gx-h/gx_core/p0/cli.py#L34) · [引导 hook](../../gx-h/gx_core/p0/cli.py#L143) · [编辑 hook](../../gx-h/gx_core/p0/intent.py#L464) · [现有确认范围](../../gx-h/gx_core/p0/intent.py#L373) · [确认及影响复核](../../gx-h/gx_core/p0/intent.py#L504)

硬预算引用同一 Budget 账本。项目资金额度内可重新分配子额度；每次准入仍受当前 PI 硬上限、父级可用额度和既有预留约束。旧 Root 维护入口可申请恢复到 profile 额度并消费已有的精确批准，发行默认 standing rules 为空。P2 的 PI 上限提高必须绑定当前版本的人类授权；旧批准、较高 profile 或新预算阶段均不能单独授权。原有支出与在途结算按实保留。原生 token 预留约束 GX 准入；provider 逐轮事后回报，实际用量可超出预留。若 PI 要求不可超出的实际 provider 总量而后端无法预先执行，GX 拒绝该次启动并注明单位。[额度调整](../../gx-h/gx_core/p0/operator_budget.py#L132) · [子额度资金](../../gx-h/gx_core/budgets/allotment.py#L13) · [旧维护入口](../../gx-h/gx_core/orchestration/service.py#L1919) · [默认规则](../../gx-h/config/approvals.json#L5) · [实际用量边界](../../gx-h/gx_core/p0/meter_limits.py#L43)

## New · Measurement、GX 裁决与 PI 签署

Evaluator 返回有类型的 Measurement，包含指标名、有限数值、单位、有效性和来源引用。语义类别限定为直接测量、数值验证、模型派生代理、启发式指标、实验结果。GX 依据实际调用核对生产者及其已批准的摘要，绑定 Run、GraphVersion、节点、Attempt、适用的 Job 和输入 Artifact 摘要。GX 校验单位、类别、来源和完整性，再用已冻结的 Goal 比较规则写入 computational verdict。无类型的 pass、生产者返回的 `accepted`、未批准的替换均按无效输入处理。缺测或无效测量保留原因，不能形成支持或反驳结论。相同输入重放返回原裁决，准则改变时从原测量产生关联的新裁决。[现有插件接口](../../gx-h/gx_core/plugins/runtime_context.py#L165) · [grid 原始测量复核](../../gx-h/workflows/science_grid_v3/contract.py#L134) · [原输入复评回执](../../acceptance/runs/science/test_criterion_changes_before_original_evaluation_commits.json#L19)

科学状态初始为 `PENDING_HUMAN`。例如，模型代理指标超过阈值可得到 computational PASS；结合或活性主张仍须满足该 Goal 所列证据条件。实际 PI 通过可信的人类入口签署或拒绝，GX 将身份、主张、Goal 版本及摘要、计算裁决和证据集摘要一同写入原有操作员回执与事件。证据或主张改变时，原签署只关联原版本。独立模型审阅和 Task 提交仍有各自的证据价值。已有本地 CLI 在受信进程中识别操作员，测试脚本也可驱动；自动化回执标明 `SCRIPTED`，正式结论须有真实人类签署记录。[证据层级](../../gx-h/contracts/labels.json#L72) · [本地操作员边界](../../gx-h/gx_core/p0/operator.py#L20) · [历史脚本身份](../../acceptance/runs/intent-cli-confirmations/summary.json#L113)

## Deferred · 验收与迁移边界

本阶段只裁定接口。A04 验证两类 Codex 会话及插件通过显式字段、别名和 hook 放宽阈值或增加预算时，GX 拒绝整项提交。A08 对照原始数据核验 Measurement 与 GX 裁决，并拒绝无类型 pass。A09 在初建、amendment 和冷恢复验证未批准的生产者变更被拒。A22 验证代理达标时科学状态仍待签。可用脚本验证签署权限和摘要新鲜度，真实湿实验和具名 PI 签署随实际项目留证。

历史 Run 继续用原 pin，保留旧判定和账目。此 ADR 复用现有表的版本化文档和事件，无需回填历史 Goal、Measurement 或签署。回退时恢复静止点的旧程序和状态副本，另存升级后已发生的 Job、结果与费用供对账。
