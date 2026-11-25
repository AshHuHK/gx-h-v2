# GX-H v3.1 的适用范围

## 采用这次修订的理由

原 v3 的直接对照中，364 次模型响应有 129 次发生在该 Attempt 的科研提交已结束之后。原生动态工具调用是一个可以利用的等待点。GX 可以先原子保存科学裁决，再让 Codex 按原 turn 结束，省去为读取已确定的结果再次调用模型。修复次数按调用身份落在单独事务，解决了结构失败随原事务回滚的问题。两处做法沿用现有任务、租约与预算所有者。[逐响应归因](acceptance/v31/attribution/historical-attribution.json) · [故障注入](acceptance/v31/terminal-protocol.json)

## 公开边界

- **传输。** 固定版 App Server 没有能原子证明动态 RPC 仍挂起并立即回复的接口。回执不明时，GX 查询同一 turn 并有限重试中断，不能凭超时补写普通工具回复。仅有客户端 `send` 或中断 ACK 时仍需核对科研提交、官方 turn 终态和计量。[提交与恢复代码](gx-h/gx_core/p0/terminal_products.py)
- **用量。** 原始事件未提供的字段记为空值。Codex 会把某些可选详细计数的缺失归一为零；在线归因保留这种不确定性。请求的模型及 provider 与可由响应直接证明的模型身份分开记录。预算扣费继续走原累计计量和原生子线程对账。可显式导出逐响应归因：`PYTHONPATH=gx-h python -m gx_core.p0.response_attribution <状态目录>/gx.db <run_id>`。[归因实现](gx-h/gx_core/p0/response_attribution.py)
- **评估规模。** 内联评估只在有界输入上使用标准库，对照 CPU Job 的原始测量及评估记录。超出本版内联容量的路径保留引用，标为尚无法判定，转独立审阅。此版本验证了现有 `fixture-grid@1` Job，不把大规模数组的专用重评能力计入本次签收。[版本化评估](gx-h/workflows/science_grid_v3/products.py)
- **版本选择。** 新能力通过 `science_grid@3` 选择，依赖固定的 Codex 0.169.0、生成的 Schema 和 Python 环境。此开发目录复用了本机 v3 锁定的这三项依赖。开发测试使用旧项目数据库的副本及独立新目录。v3、v2.1 的源码和原项目仍可按原版本运行。生产切换与原地降级没有在本次开发分支上实施。
- **B4。** 目前只有这一个工作流实际使用这套合同与评估接口。按修订包推迟通用 Product/Evaluator 插件 API 的抽取；出现第二个真实调用方时再核对所需接口。这里不对其他科研领域、模型或 provider 作效率或质量结论。

## 独立的两轮科研质量验收 B5

预先冻结的问题是在有限坐标集合上比较二次插值与分段线性插值，选择尚未测量的点，执行下一轮真实 CPU Job。两项初始计算进行时送入两个新来源，一项限制下一轮可选坐标，另一项声称 x=1 的分数为 0，与本次测量的 −1 冲突。独立验收者使用原始观测和有理数计算重建两种预测，核对实际工具参数、科研产物以及先后顺序。[冻结实验及判据](acceptance/v31/quality/case.json) · [独立复算程序](acceptance/v31/quality/independent_reader.py)

首次 B1 实测在五个原生 turn 的上限内没有产生最后的 planner 决策。该项目实际花费 420,896 token，完成 3 个 Job。两条路线推荐的坐标分别为 2.125 和 1.8125，实测为 −1/64 和 −9/256，相差 +5/256。旧 planner 先把独立审阅引用填入限定为一手证据的字段，再把下一轮网格提交为裸数组，两次拒绝耗用了可用 turn。该次结果保留为未闭环。[B1 独立验收](acceptance/v31/quality/independent-review-b1.json)

后续修复将一手证据、独立审阅和补充来源的字段用途写入 planner 实际收到的工具合同，并在发布 Artifact 前校验下一轮网格形状。网格结构错误可在同一 Attempt 修复。该合同随新领的多轮或有补充来源的任务冻结；单轮无补充来源的效率样例沿用原先的线格式。[planner 实现](https://github.com/AshHuHK/gx-h-v2/blob/47fc728d32bc10ba478df6a063529a8d98f98828/gx-h/gx_core/p0/grid_planner.py)

在修复后的产品 `9cd6655` 上，用相同的 case、冻结 grader 和五个 turn 上限新建项目。4 个原生 turn 的首次提交均被接受，3 项 CPU Job 成功，4 笔 provider 回执共 293,439 token，无估算。两项资料都在初始 Job 运行期间进入项目。第二轮的实测确认二次插值选点优于线性插值选点，差值为 +5/256。最终决策同时记录了反例、剩余两个未测坐标及连续最优值的不确定性。19 项预定检查均通过。B1 的未完成记录仍可逐项对照。此结论限于一项合成数值研究，两种方法使用同一组初始观测。[科研结果及模型产物](acceptance/v31/quality/final-live/evidence.json) · [原始准则逐项验收](acceptance/v31/quality/final-live/grade.json) · [修复过程线路对照](acceptance/v31/quality/planner-transport-proof.json)

独立验收者未参与出题或生成模型产物，使用原始提交和官方运行记录核对了四个工作线程、全部输入引用、Job 的时间顺序及两种方法的独立数值复算。最终实测通过 8 项科学判断，另行标记了脚本阳性对照。一版各一次真实采样，足以验收这个闭环；模型采样、planner 合同和简报修改各自对科研质量的贡献仍无法分离。[独立验收原件](acceptance/v31/quality/independent-review-final.json)
