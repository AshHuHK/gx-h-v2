# GX-H v3.1 科研执行开发分支

新项目可在研究规格中设置 `"workflow": "science_grid"`、`"workflow_version": "3"` 和 `"plugins": ["org.gx.science-grid@3.0.0"]`，然后通过原有 `bin/gx-p0 start` 入口启动。可以先执行 `bin/gx-p0 plugin verify org.gx.science-grid@3.0.0` 核对安装。已有项目按各自保存的版本恢复。开发分支的模型请求、结算、科研证据和启动示例见本版验收记录。[源码与基线](V31_SOURCE_AND_BASELINE.md) · [提交与科研工作流](V31_B1_SUBMISSION_PROTOCOL.md) · [角色与简报效率](V31_B2_B3_EFFICIENCY.md) · [适用范围与科研质量](V31_LIMITATIONS_AND_NEXT_STEPS.md)

本次验收的产品提交为 `9cd6655`。同题 13 波完成了 26 个项目；另一个带中途补充资料的两轮数值研究通过独立复算。13 波中一笔最终响应缺少用量回执，历史账本保留估算。原始测试和分组对照记录在 [验收目录](acceptance/v31)。

## v3 插件平台

在 v3 安装目录中，操作员可以按版本安装本地科研插件。GX 校验审核过的包内容，在独立 Python 命名空间中执行 `register(ctx)`，并列出插件贡献的工作流、工具和计算能力。现有项目继续使用各自保存的工作流和科研记录。

首次部署可使用 [Linux x86_64 离线包](dist/gx-h-v3-p1-linux-x86_64.tar)，按[安装与启动步骤](V3_DEPLOYMENT_REPORT.md)在独立目录运行。该包对应产品提交 `2476f2b`。P0 已签收，P1 的科研结果和冷恢复已验证，真实模型的尾延迟仍待签收。稳定版本保留在 v2.1。[阶段验收](V3_ACCEPTANCE_REPORT.md) · [回退操作](V3_ROLLBACK_REPORT.md)

```bash
source .venv/bin/activate
bin/gx-p0 plugin digest examples/plugins/hello_research
bin/gx-p0 plugin install examples/plugins/hello_research --sha256 d425088e9bd646bb34256bd2aee4f2c9029587d7534187400093993bd6b46603
bin/gx-p0 plugin verify org.example.gx.hello-research@0.2.0
```

示例命令的摘要对应本版本随附的 hello_research。发布自有插件时，使用已审核目录的摘要。插件的嵌套模块、工具参数和注册函数可直接参考这个例子。[插件源码](examples/plugins/hello_research/plugin.py) · [插件安装与核验](examples/plugins/README.md)

## v2.1 科研执行基础

官方 Codex App Server 管理模型、原生子代理和会话。GX 保存研究目标、多个工作流的长期任务、Job、科研产物、审查、决策及预算。操作入口为 `bin/gx-p0`。

## 在 v2.1 启动和恢复

在本目录使用已经部署的 Python 环境。以下示例在同一项目中创建两个独立 Run，分别保存工作流版本、任务、Job 和预算。缺少推理凭据时仍可查看状态和使用本地操作命令。

```bash
source .venv/bin/activate
bin/gx-p0 login-status
bin/gx-p0 start --state ./var/study --run-key sampling-a --demo-grid
bin/gx-p0 start --state ./var/study --run-key sampling-b --demo-grid
bin/gx-p0 run --state ./var/study --timeout 3600
bin/gx-p0 status --state ./var/study
```

`status` 列出每个 Run 的 `run_id`。把以下 `RUN_ID` 换为要操作的那一个。在另一终端暂停时，其他 Run 可以继续。重启后再次执行 workspace 的 `run` 可接管仍在运行的工作。显式暂停的 Run 需先恢复。到达命令的墙钟上限时返回码为 75。

```bash
bin/gx-p0 operator pause --state ./var/study --run-id RUN_ID
bin/gx-p0 operator usage --state ./var/study --run-id RUN_ID
bin/gx-p0 operator resume --state ./var/study --run-id RUN_ID
bin/gx-p0 run --state ./var/study --timeout 3600
```

已耗尽模型预算时，`operator budget-preview` 和 `operator budget-apply` 也可本地运行。先用 `--resource tokens --limit 数值 --budget-scope run` 查看变更，再按回执提交对应的预览 ID 和摘要。团队状态由 `team --state DIR --run-id RUN_ID` 查看。普通自然语言交流走模型会话。

为自定义 spec 设置项目总预算用 `workspace_budget`，给单次研究分配额度用 `run_budget`。均可分别指定 `tokens`、`api_calls`、`job_submissions`、`cpu_seconds` 等预算行。未显式指定时，带 key 的新 Run 按可下拨余额分配默认份额。项目实耗、实际在途预留与可下拨余额可在 `status` 的 `workspace_budget` 中分别查看。[带两个团队的可运行规格](acceptance/v21_long_run.py)

安装材料、已部署 staging 的位置、验收命令与适用范围见 [v2.1 部署](V21_DEPLOYMENT_REPORT.md)、[验收矩阵](V21_ACCEPTANCE_REPORT.md)、[限时连续运行](V21_LONG_RUN_REPORT.md)、[故障注入](V21_FAILURE_INJECTION_REPORT.md)、[回退](V21_ROLLBACK_REPORT.md) 和 [已知限制](V21_KNOWN_LIMITATIONS.md)。

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

## v2.1 离线安装包

本次 Linux x86_64 部署包位于 `dist/gx-h-v2.1-linux-x86_64.tar`，旁边附 SHA-256 校验文件。源码与锁定版本的官方二进制、Schema、Python wheels、科研坐标、独立复算代码均随包交付。在具备 CPython 3.11 和 Node 22 的主机上，可解包到新目录。通用分析能力另需系统 libseccomp。

```bash
cd dist
sha256sum -c gx-h-v2.1-linux-x86_64.tar.sha256
mkdir gx-v21-install
tar -xf gx-h-v2.1-linux-x86_64.tar -C gx-v21-install
cd gx-v21-install
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index --require-hashes --find-links release-assets/python -r release-assets/python/requirements.lock
python scripts/release_assets.py all
bin/gx-p0 login-status
```

## 在数值网格项目使用插件

下面的命令沿用随包的数值研究问题，并为新 Run 选择网格插件。工作节点能通过 `gx_grid_measurement` 读取自己负责的路线、冻结阈值和实测结果。

```bash
mkdir -p var
python - <<'PY'
import json
from pathlib import Path
spec = json.loads(Path("examples/paired-grid.json").read_text())
spec["plugins"] = ["org.gx.science-grid@2.0.0"]
Path("var/paired-grid-plugin.json").write_text(json.dumps(spec))
PY
bin/gx-p0 start --state var/research-v3 --run-key paired --spec var/paired-grid-plugin.json
bin/gx-p0 run --state var/research-v3 --timeout 3600
bin/gx-p0 status --state var/research-v3
```

[网格插件](gx-h/plugins/installed/org.gx.science-grid/2.0.0/plugin.py) · [分阶段验收](V3_ACCEPTANCE_REPORT.md)
