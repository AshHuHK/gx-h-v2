# GX Harness

A local harness for long-running projects with Codex-native agent teams.

GX 将用户的一份 Markdown 任务书变成可跨会话推进的项目。Codex 创建 Root、模块负责人和 worker 线程，让它们讨论、分工、复核。GX 记录每一项任务的归属、产物、待办和最终决定。关掉终端后，可以用同一个项目名和状态目录接着做。

内置的 `science_grid@1` 工作流用本地 CPU Job 检验合成数值假设，独立 worker 评估原始观测并决定下一轮实验。`module_collaboration@1` 用于有独立交付件的模块协作，随库示例提供了两个服务窗口的 16 条等候时间观测。

## 第一次使用

在 Linux 或其他兼容的 POSIX 环境中，从仓库根目录运行。离线检查使用 Python 3.11 和 SQLite。下面的命令先检查一个全新项目的状态，然后初始化其本地数据库。

```bash
bin/gx-project --project hello-gx --state-dir ./state/hello-gx --status
bin/gx-native --project hello-gx --state-dir ./state/hello-gx --init-store
```

第一条在新目录上返回 `NEEDS_PROMPT`。第二条返回数据库初始化回执。这两步可以在没有 Codex 登录的机器上运行。

## 用 Codex 派发真实任务

本版的宿主适配针对 **Codex CLI 0.169.0** 的 App Server stdio 和原生子 agent 工具。请让 `codex` 在启动服务的 `PATH` 中可用，先在本机完成 Codex 登录，并确认账户可用所选模型。默认模型为 `gpt-6.1-sol`，新项目也可通过 `--model gpt-6.2-astra` 选择另一项已配置策略。原生运行的代理设置见下表。

| 环境变量 | 本版默认值 |
| --- | --- |
| `HTTP_PROXY`、`HTTPS_PROXY` 及其小写形式 | `http://127.0.0.1:7890` |
| `ALL_PROXY` 及其小写形式 | `socks5h://127.0.0.1:7891` |
| `NO_PROXY` 及其小写形式 | `localhost,127.0.0.1,::1` |

两个本机代理端口必须可用。模型调用使用本机 Codex 的登录和传输。GitHub 仓库访问凭证与科研项目的运行配置相互独立。

```bash
bin/gx-project --project queue-study --state-dir ./state/queue-study \
  --collaboration module-leads \
  --prompt-file examples/module_collaboration/question.md \
  --budget-file examples/module_collaboration/budget.json \
  --max-sessions 4 --max-wall-seconds 3600 --background

bin/gx-project --project queue-study --state-dir ./state/queue-study --status
```

后台启动回执表示项目驱动已经启动。用第二条命令查看项目是否就绪、正在等待、受阻或已经结案。继续原项目时沿用同一 checkout、项目名和状态目录。首次任务书和预算已经保存，续做命令可以省略它们。

## 从研究目标启动实验

本地合成评分样例由 Codex Root 制定可证伪的预测、采样点和前瞻评价阈值。任务书包含计算预算与运行输入示例，需要可调用的 Codex 模型及匹配的原生 worker。首次启动、查看状态和恢复原项目分别执行：

```bash
bin/gx-project --project grid-science --state-dir ./state/grid-science --collaboration auto \
  --prompt-file examples/science_native/question.md --max-sessions 4 --max-wall-seconds 3600
bin/gx-project --project grid-science --state-dir ./state/grid-science --status
bin/gx-project --project grid-science --state-dir ./state/grid-science
```

保留状态目录，驱动程序会继续原项目、原件和在途 Job。每轮评估与是否继续由两个不同的 worker 提交。项目等待期间可从另一终端查看状态。受控协议验收可用 `python3 -B -m unittest tests.test_science_native_entry -v` 重放 Root 提案、执行真实 CPU Job，并核对原始输出。[研究任务书](examples/science_native/question.md)

## 在浏览器查看同一个项目

```bash
bin/gx-web --project queue-study --state-dir ./state/queue-study --port 8080
```

打开终端打印的本机地址。网页读取项目快照和里程碑，在现有项目收件箱保存追加要求。浏览器重新连接时，SSE 事件游标用于补齐断线期间的进展。此入口绑定本机环回地址，供单项目使用。[本机网页及接口](web/README.md)

## 谁负责哪件事

| 环节 | Codex 与 GX 怎样配合 |
| --- | --- |
| 派工 | Root 或模块负责人从 GX 就绪清单选择任务，通过 Codex 原生工具派出 worker。GX 将实际线程与任务租约关联。 |
| 交付 | worker 发布原件并调用 `gx_task_submit`。负责人可以通过线程消息得知进展，通过 GX 回执确认哪个版本已交付。 |
| 复核与返工 | 模块负责人汇总有引用的结果。Root 针对本轮交接记录接受、指定模块返工或停止的决定。已接受的其他模块可以复用。 |
| 长程恢复 | 项目驱动观察本地状态。有就绪工作或反馈时继续 Root，健康等待期间留在本机。终态项目可以直接读取原结果。 |

详见[架构与源码导读](docs/architecture.md)、[模块协作工作流](workflows/module_collaboration/)和[示例任务书](examples/module_collaboration/question.md)。

公开的 `native_core` 目录向 Codex 提供 18 个项目、团队、任务、产物和记忆工具。仓库内另有通用 CPU Job 执行与结算代码，可由后端调用。随库测试实际启动本地子进程并检查输出与预算，原生工具目录的范围见[宿主桥接](gx_core/hostobs/native_app_server.py)和[本地 Job 测试](tests/test_local_job.py)。

项目状态目录解锁后，可将指定 run 已受理的文本产物加入记忆索引。随后可用 `gx_memory_search` 依项目查找，按原 `ArtifactRef` 核对内容。将下面的 `RUN_ID` 替换为项目状态返回的 ID。

```bash
python3 -m gx_core.knowledge.index_run --state-dir ./state/queue-study --run-id RUN_ID
```

## 离线验证

```bash
python3 -B -m unittest discover -s tests -v
python3 -B adapters/hosts/generate.py --check
```

协议测试使用受控的 App Server 传输。工作流测试使用临时 SQLite 状态，覆盖派工、提交、模块返工和原件复用。实测在线模型能力时，另行使用上面的 `gx-project` 原生入口。
