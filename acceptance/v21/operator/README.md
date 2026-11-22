# 本地操作员验收

`bin/gx-p0 operator --help` 列出此版本的命令。`--state` 指向工作区，工作区包含多个 run 时用 `--run-id` 选择目标。GX 使用本机操作员身份及其项目范围，保存每条结构化请求的 ID、动作、结果摘要和事件。`--message-id` 可用于重放同一次写入。状态、用量和审计查询展示读取时的状态。

```sh
bin/gx-p0 operator pending --state "$STATE" --run-id "$RUN"
bin/gx-p0 operator usage --state "$STATE" --run-id "$RUN"
bin/gx-p0 operator budget-preview --state "$STATE" --run-id "$RUN" --resource tokens --limit 120000
bin/gx-p0 operator budget-apply --state "$STATE" --run-id "$RUN" --preview-id "$PREVIEW" --preview-sha256 "$DIGEST"
bin/gx-p0 operator audit --state "$STATE" --run-id "$RUN" --output audit.json
```

默认预算预览针对 run 的实际分配。带父预算的 run 会在同一事务中更新自己的额度和父级资金预留，增长需要工作区有足够余额。独立 run 提高额度复用 GX 预算审批记录。对已经花费或正在使用的资金设置新的后续准入上限时，使用 `budget-preview --budget-scope project`。此时 apply 的 `--preview-id` 来自 Intent 预览中的 `change_id`。

`message --action file.json` 直接处理结构化 Intent 或操作员动作。`--queue-only` 保留排队语义。驱动程序和 `operator recover` 可以处理已排队的结构化操作。`message --text`、`message --supplement` 和默认单 run 启动继续经过官方主线程。使用新 `--run-key` 启动时，初始结构化 Intent 由 GX 本地保存。科学工作由正常运行入口推进。

## 验收记录

- [v2 原始复现](before.json) 记录了结构化 CPU 和 token 限额命令各触发一次主线程回合，以及 token 耗尽后调高额度的请求停在 pending。
- [v2.1 公开入口](after.json) 记录命令、逐次累计用量、真实 turn ID、被占用的 native 锁下的操作结果，以及两个 run 的分配变更。模型端使用 localhost Responses fixture，运行时是官方 Codex App Server 0.169.0。
- [v2 只读库](v2-readonly.json) 记录公开 status 在升级前读取旧库时的前后 SHA-256。
- [重放程序](check.py) 在临时工作区初始化数据。执行 `.venv/bin/python acceptance/v21/operator/check.py --output /tmp/gx-operator.json` 可生成独立记录。
