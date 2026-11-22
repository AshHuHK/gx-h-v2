# GX + Codex Autonomous Science Harness

本目录是一套独立可运行的科研执行系统。官方 Codex App Server 负责模型、原生子代理和会话，GX 负责长期任务、租约、科研 Job、不可变产物、证据和决策。发布入口是 `bin/gx-p0`。在线执行使用官方 ChatGPT OAuth。

## 启动一个项目

从本目录运行以下命令。示例生成两条合成数值路线；分别派发审查和决策 Root，并由 GX 推进下一轮。项目可随时用同一个状态目录继续。

```bash
./bin/gx-p0 login-status
./bin/gx-p0 doctor
./bin/gx-p0 start --state ./var/my-first-project --demo-grid
./bin/gx-p0 run --state ./var/my-first-project
./bin/gx-p0 status --state ./var/my-first-project
```

使用自己准备的 `--spec project.json` 可设定研究问题、假设路线、判别阈值与轮数。在另一终端可用 `stop --state DIR` 暂停调度，再用 `resume --state DIR` 恢复。主对话可接收 `message --text`、`--supplement` 或精确的 `--action` 文件。不同项目分别使用自己的状态目录。[部署与授权](DEPLOYMENT.md)

默认模型为 `gpt-6.2-astra`。协调者使用 `ultra`，复杂推理、规划、代码和科学审查使用 `max`。简单任务和测试使用 `xhigh`。用 `GX_P0_TEST=1` 启动验收项目可把实际模型 turn 统一设为 `xhigh`。[模型目录与实测](audit/OAUTH_EFFORT.md)

## 交付与证据

- [验收报告](ACCEPTANCE_REPORT.md) 与 [故障注入报告](FAILURE_INJECTION_REPORT.md)
- [已知限制](KNOWN_LIMITATIONS.md) 与 [部署、状态与回退](DEPLOYMENT.md)
- [源码审计](AUDIT_REPORT.md)、[基线](BASELINE_REPORT.md)、[复用与改造](REUSE_DELETE_REWRITE.md)、[架构决定](ARCHITECTURE_DECISIONS.md) 和 [实施计划](P0_IMPLEMENTATION_PLAN.md)
- [原始实施提示](GX_Codex_Implementation_Agent_Prompt.md)、[设计约束](DESIGN_SPEC.md) 与 [验收合同](acceptance/ACCEPTANCE_MATRIX.md)
