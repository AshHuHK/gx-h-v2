# v2.1 P0 运行时依赖闭包

候选 `a05dcef64522fbbbc12120b1d78da518e6886b10` 以公开入口 `bin/gx-p0` 运行，静态清点 237 个项目内 Python 源文件和程序入口，从 P0 可达 200 个，五个兼容入口的并集可达 224 个。真正执行的候选 P0 源文件为 173 个；只运行冷启动 `--help` 时为 95 个。五个旧入口 `--help` 实际执行 94 个源文件。各组有重叠。现场覆盖了真实 v2 项目被候选恢复、继续数值工作和冷终态重开。[逐文件静态边、执行、打开与子进程记录](acceptance/v21/deployment/closure.json) [原始进程事件](acceptance/v21/deployment/audit-events.jsonl.gz)

| 类别 | 内容与边界 |
|---|---|
| P0 必需（182 个已归类源文件） | CPython、SQLite、GX P0 CLI/Driver/Roots、原生 App Server 客户端、现有事务、预算、Task/Lease、Outbox、Job/Artifact/Manifest 与相应合同/迁移。配置所选工作流、其钩子和能力也有自己的必需代码。逐文件 `p0_static`、`observed_cold_cli`、`observed_candidate_execution` 区分入口、条件路径和本次实际执行 |
| 兼容（41 个已归类源文件） | 原生项目、Web、旧检查命令集中在 `gx_core.cli.compat`；旧工作流适配、science_inputs、science_refresh_v2 等仍参与旧工作流精确 hash 和续跑。历史 TeamService / TeamManager 分配和 brief 扩展保留旧记录读取。旧 CLI 检查了 5 个帮助入口，其完整工具目录、回放与有状态探针另有实证 |
| 可选（14 个已归类源文件，另含非 Python 运行时） | 其他宿主观测、可选检查、例程、生成器和手动知识索引按调用使用。独立结构复算需要固定的 Biopython 1.88 和 NumPy 1.26.4。受限分析 worker 使用 Linux libseccomp；GPU/外部能力按各自资源与环境选择。选用 Cordis 评分器要 Node 和四个锁定 npm 包。DSH SDK 是额外的运行方式 |
| 历史材料 | `gx_reference/`、`codex_adapter/app_server.py`、`audit/`、`acc_1/` 是早期参考与验收材料；`acceptance/runs/` 的 schema 清单仍供发行安装校验使用。项目保留这些材料。分类中记录了参考用途和例外 |

当前 staging 在安装好依赖后从候选物理移开 Cordis 的 `node_modules`，默认 Grid 仍从原数据库完成；验收随后将依赖放回部署目录。启用评分器时，已安装的 Cordis 4.1.0、cosmokit 1.9.0、dsh-scope 0.2.2-rc.5 和 standard-schema/spec 1.1.0 对应的原始 Node 用例实跑 2 项通过。未选择 DSH SDK / GPU 的本次 Grid 有真实 CPU 成果。新项目计量默认使用原生回执；私有 rollout 读取仅在显式选择或旧付费历史的兼容补证路径启用。其他 profile 可形成不同的可达集合。[部署依赖和后端](acceptance/v21/deployment/staging.json)

## 动态分派与兼容边界

- 入口冷启动执行 `gx_core.p0.cli`。原生项目/旧 Web 的模块沿各自 `gx-h/bin/` 的兼容入口加载。兼容服务专项保留了 28 个 legacy tool 的目录和真实的旧 Native/Project 命令行为。[兼容对照](acceptance/v21/service/after.json)
- 生成 Task brief 会按注册表读取旧 team_assignment 扩展。在本次恢复及终态重开中，旧 team 文件实际调用的函数为 `team_assignment_section`；Python 另执行了 TeamManager 的类定义。调用观测未见构造实例或执行 manage、message、snapshot。旧分配仍有读者，不能依据冷启动时未加载它就移除。[完整调用/执行阶段](acceptance/v21/deployment/closure.json)
- 工作流注册表从本地清单装载包和已固定的 compat hook。JobService 通过能力清单选择 parser 和 entry_script。私有 venv 中的解释器在 Driver、`python -I` runner 和实际 worker 内应用 Python audit hook。记录区分代码执行、文件打开尝试与进程启动，同时对候选的兼容 Team 方法作窄范围调用观察。真实观察的子进程包括正式 Codex app-server、GX runner、adapter_host 和数值 worker。[静态及动态测量程序](acceptance/v21_closure.py)
- G01 复验记录原来三个 Job 的启动 claim、scheduler_ref、runner 身份和终态文件，及候选唯一新增的第四个 Job。进程审计中每个条目的计数是观察该项的不同 Python 进程数，实际 Job 数由 Job 记录和单次启动 claim 给出。[G01 原库与启动凭据](V21_ROLLBACK_REPORT.md)

## 复算

正式 checkout 中的源码、Git 对象及本机 staging 可直接复算逐文件结果。压缩追踪只保存阶段、PID、父 PID、事件种类和项目相对路径或子进程名。验收退出时卸下私有 venv 中的观测钩子。排序后的报告可逐字节比较。

```sh
.venv/bin/python -B acceptance/v21_closure.py \
  --install "$PWD/var/v21-staging/a05dcef/install" \
  --trace acceptance/v21/deployment/audit-events.jsonl.gz \
  --output /tmp/gx-v21-closure-recomputed.json
cmp acceptance/v21/deployment/closure.json /tmp/gx-v21-closure-recomputed.json
```

静态分析包含相对导入、包初始化、旧 shell 入口、注册 provider、按名加载的 owner、工作流/能力清单和单独运行的 Python 文件。详细表保留 6 处 Python 动态装载调用与 24 个清单项。可达仅说明代码或受支持配置有依赖路径；打开文件只说明尝试读取，其业务执行另由运行事件或 Job 收据核对。观测范围是上述入口和有真实状态的续跑，其他选项使用各自的验收记录。[初始环境及锁](acceptance/v21/deployment/environment.json)
