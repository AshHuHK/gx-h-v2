# 两个酵母研究 Run 的并发与交接

两个独立 Goal 的科研模块实际并发。2026-11-27 的第一次跨 Run 交叠发生在 **01:30:35.110–01:31:09.831 UTC，共 34.721 秒**。海藻糖 Run 为 `gxr_01m8d9q2sexhdhq33k`，蔗糖 Run 为 `gxr_01m8d9xyhw3b1ftg09`。两处部署均强制现有 ChatGPT 登录；证据来自 GX 已确认的任务投递及官方 Codex 0.169.0 中有模型响应的回合。JSON 保存各自的任务、线程、模型、起止事件和 rollout 行号。所选区间的结束帧是 `turn_aborted`，用来界定实际重叠时长；科研行动另据 GX 已生效的决定和作业核实。[并发与交接回执](evidence/field-collaboration.json)

PI 请求文献路线核对具体底物、实验材料和反应，并允许其他路线继续。这句话于 **01:26:24.332 UTC** 在海藻糖 Run 的事件 62 入箱，落在该 Run 两个模块 **20.536 秒**的回合交集内。事件 201、202 分别记录定向反馈和 Main 的分句路由。送达回执只对应受指派的文献模块。该研究者在决定 651 采纳，事件 652 记录理由，随后一项全文取得作业和两项补检索全部成功。下一次文献模块决定 946 明确引用三个具名输出，并委托三项后续工作。[PI 原话、行动与后续引用](evidence/field-collaboration.json)

这次快照分别读至海藻糖事件 1089 和蔗糖事件 1729，此时两个 Goal 都处于 OPEN。海藻糖已有模块内“PI 意见 → 作业 → 引用产物 → 下一次决定”的回执，尚无顶层 Lead 引用这条 PI 链的结题决定。蔗糖事件 1416 是由 GX 产生的部分报告，已释放给 Lead，截取时未见 Lead 根据该报告内容采取行动。两项研究的最终科学结论仍以各自实际封存的报告为准。

## 复核

在产品根目录运行以下命令。它只读部署的 SQLite 状态、Codex 会话历史以及部署配置的模型/登录方式字段。输出文件可自行指定。运行中的项目会继续新增事件，较晚重放得到的报告状态可能前进。

```bash
.venv/bin/python -B acceptance/science_capabilities/field_collaboration.py \
  ../gx-science/capabilities-check/live-trehalose \
  ../gx-science/capabilities-check/live-sucrose \
  --output /tmp/gx-field-collaboration.json
```
