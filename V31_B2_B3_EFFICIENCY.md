# 角色工具、工作简报与效率

B2 单独调整 `science_grid@3` 的工作角色。独立审阅者和 planner 默认可取当前任务、读取 Artifact、查看已配置的网格测量并提交结果。研究人员与执行人员保留其职责所需的能力与 Job 工具；明确配置的独立计算或原生委派由现有角色配置承接。GX 同时设置 Codex 官方 `agents.enabled` 与相关功能位，因为仅设置功能位时，模型目录仍可能暴露委派。Codex 基础指令沿用所选模型的原件。[GX 角色配置](MODEL_PROFILES.md) · [实现](gx-h/gx_core/p0/profiles.py)

上一轮 v3 的 13 波真实 reviewer 共调用任务上下文 73 次、Artifact 读取 466 次、网格测量 104 次、提交 76 次，planner 对应为 26、234、14、26。B2 默认对这两个角色保留了四项实际用到的能力。数值取自回调日志，包含同一响应内发起的多个调用。[v3 实测归因](acceptance/v31/attribution/historical-attribution.json) · [角色和实际调用核对](acceptance/v31/b2/role-catalogs.json)

## 简报与新鲜度

B3 把已经授权且内容不可变的小型 JSON 摘要按引用及 SHA-256 放进已有的工作简报。单项和总预载均限 16 KiB，数组、原始观测和大型数据仍可按需读取。首个用户 turn 携带简报，开发者指令只包含角色指导。恢复后的工作线程仍可以按当前授权获取更新，避免把旧消息当作最新资料。

对上轮真实 v3 的 78 个已领任务重放简报选择，共有 338 份当时确实被读取的摘要逐一通过原始字节及 SHA-256 校验。此次选取的最大序列化预载为 4,722 字节。上述数字衡量历史工作负载的可预载性；实际少发了多少模型请求、用时和 tokens 以分组测量为准。[逐任务重放](acceptance/v31/brief-replay.json)

## 测量口径

13 波的科研题、预算和两侧数据取自原对照。v3.1 在上一波完成后立即开放下一波，取消了旧协议每 10 分钟释放一次的固定等待。对照使用每项目时长、活跃执行时长和每个匹配输入的 token 数；总试验跨度单列。B2 的 GX 指令 A/B 保持工具及宿主配置一致；B3 的对照使用相同的 B2 角色策略。[预先保存的比较方案](acceptance/v31/waves/measurement-plan.json)

## 同题双项目真实 OAuth 对照

每个条件使用同一组东西两侧输入，各有两次原生审阅和两次规划。表内时间是两个项目各自耗时之和。B1 这一对与另一个质量试验的模型运行有约 57 秒重叠，其余三组没有该重叠。样本量只能支持这组任务的判断。[原始回执及逐项目对照](acceptance/v31/waves/comparison.json)

| 条件 | 完成响应 | 总 token | 两项目耗时之和 |
|---|---:|---:|---:|
| B1，原角色范围及原 GX 指令 | 12 | 279,587 | 159.558 秒 |
| B2，按角色精简工具，沿用原 GX 指令 | 12 | 256,582 | 104.749 秒 |
| B2，按角色精简工具和 GX 指令 | 12 | 255,458 | 119.093 秒 |
| B3，同上并预载小型 Artifact | 8 | 162,246 | 78.101 秒 |

B2 整组 token 减少 8.63%，两项目耗时之和减少 25.36%。单看 GX 指令，在工具目录、委派和官方基础指令相同的条件下，首请求输入中位数从 19,232 降到 19,112，总 token 少 1,124，耗时增加 14.344 秒。当前数据支持角色工具精简；短指令本身的加速没有得到证实。[指令对照](acceptance/v31/waves/b2-instruction-control.json) · [实际角色目录及委派](acceptance/v31/b2/official-role-catalogs.json)

B3 相对 B2 的两个项目都更快，合计 token 减少 36.49%，耗时减少 34.42%。18 份预载内容均与原 Artifact 匹配，两个项目的证据方向、独立审阅与规划决定均与 B2 一致。没有过期引用、重复 Job、上下文压缩或估算结算。首请求输入中位数从 19,112 变为 19,144，节省主要来自后续读取。[B2 实测](acceptance/v31/waves/b2/measured.json) · [B3 实测](acceptance/v31/waves/b3/measured.json)

| 条件 | READ 完成数 / token | PRODUCE 完成数 / token |
|---|---:|---:|
| B1 全部 13 波 | 106 / 2,397,846 | 51 / 1,275,731 |
| B2 原 GX 指令、同一对输入 | 8 / 164,460 | 4 / 92,122 |
| B2 短 GX 指令、同一对输入 | 8 / 163,477 | 4 / 91,981 |
| B3 小型预载、同一对输入 | 4 / 78,539 | 4 / 83,707 |

B1 另有一次直接提交缺少最终用量，未进入分类小计。各组原始记录分别标明实际收到的 input、cached input、output、reasoning 和 total。环回官方宿主也验证了不同科研路径。独立审阅样例的请求由 13 降至 6，耗时由 3.886 秒降到 3.215 秒。纯确定性样例由 6 降至 3，耗时由 2.895 秒增至 3.026 秒。中途补交的来源触发一次后继审阅，原先两项 CPU Job 均复用。[环回线路与迟到来源](acceptance/v31/brief-wire.json)

冷恢复时，原生 child 的短暂 interrupted 视图可能先于有终止时间的官方记录。GX 在确认终止前保留同一条子任务用量记录。在真实宿主上主动丢弃其开始通知，连续两次验证了稍后可读原 Artifact，失效租约、外部项目和写入访问继续被拒。[修复前记录](acceptance/v31/b2/child-recovery/pre-fix-observations.json) · [修复后实测](acceptance/v31/b2/child-recovery/official-lost-start-repeat.json)

最终产品 `9cd6655` 上核对了 8 个原生角色目录、19 项 child 行为和 47 项相关测试。默认 reviewer/planner 的 GX 工具从 9 项减至 4 项，目录从 22,859 字节减至 17,015 字节。允许委派的配置仍可创建原生 child，并在恢复后按有效租约读取原 Artifact。[最终原生验证](acceptance/v31/b2/child-recovery/final-official-lost-start.json) · [相关测试](acceptance/v31/b2/child-recovery/final-tests.log)
