# GX + Codex Autonomous Science Harness

本目录是一套独立可运行的科研执行系统。官方 Codex App Server 负责模型、原生子代理和会话，GX 负责长期任务、租约、科研 Job、不可变产物、证据和决策。发布入口是 `bin/gx-p0`。在线执行使用官方 ChatGPT OAuth。

## 启动一个项目

用随包的实验坐标开展 2RE9 结构研究。这个入口读取目标、来源与资源上限。研究员选择可比较的原子集合，提交假设，并在计算前冻结覆盖和比较判据。需要官方 Codex 登录、发布包中的 CPU 工具与独立复算环境。[离线安装与材料](RELEASE.md)

```bash
./bin/gx-p0 start --state ./var/tl1a-study --tl1a-geometry
./bin/gx-p0 run --state ./var/tl1a-study
./bin/gx-p0 status --state ./var/tl1a-study
```

在另一个终端使用 `./bin/gx-p0 stop --state ./var/tl1a-study` 暂停，再用 `./bin/gx-p0 resume --state ./var/tl1a-study` 从同一状态继续。`message --state DIR --text '把项目 token 上限降到 10000'` 可提交具体请求，`message --state DIR --supplement '先前对照可能受污染'` 可提交相关资料。状态中的 `supplements`、`reviews` 分别给出来源处置与实际审阅范围，`resource_limits`、`attempt_waits` 和 `unsettled_turns` 用于区分费用、等待及待对账工作。

需要复用本次扩大的预算和两模型分工时，使用随附实跑配置。`GX_P0_TEST=1` 选择验收档 `xhigh`。正式日常项目可按角色使用各自配置的档位。[实际预算规格](acceptance/runs/acc-1-research/oauth-v2/spec.json) · [两模型配置](acceptance/runs/acc-1-research/oauth-v2/model-profile.json) · [角色配置的加载方式](MODEL_PROFILES.md)

下面的快速回归示例生成两条合成数值路线，分别派发审查和决策 Root，并由 GX 推进下一轮。

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

- [ACC-1 修复、分批签收和运行证据](ACC_1_IMPLEMENTATION.md)
- [独立 Linux 发布附件、离线安装与复验](RELEASE.md)
- [验收报告](ACCEPTANCE_REPORT.md) 与 [故障注入报告](FAILURE_INJECTION_REPORT.md)
- [已知限制](KNOWN_LIMITATIONS.md) 与 [部署、状态与回退](DEPLOYMENT.md)
- [源码审计](AUDIT_REPORT.md)、[基线](BASELINE_REPORT.md)、[复用与改造](REUSE_DELETE_REWRITE.md)、[架构决定](ARCHITECTURE_DECISIONS.md) 和 [实施计划](P0_IMPLEMENTATION_PLAN.md)
- [原始实施提示](GX_Codex_Implementation_Agent_Prompt.md)、[设计约束](DESIGN_SPEC.md) 与 [验收合同](acceptance/ACCEPTANCE_MATRIX.md)
