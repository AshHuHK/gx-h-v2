# Phase 2A 实现与启动

GX 能在同一个静态 Workflow Run 内保留待决策分支，继续对账已经提交的 Job，并把其输出交给其他依赖已满足的节点。项目的正式目标、预算、产物和计量仍存放在原 GX 项目中。基线为 `v3.2-p2@51e0e13`。[改动前调用链](PHASE_2A_SOURCE_MAP.md) · [Phase 1.5 的 11 项决定](V32_PHASE_1_5_DELTA.md) · [受测提交及运行记录](PHASE_2A_ACCEPTANCE_REPORT.md)

| 原有入口 | 代码行为 |
|---|---|
| 静态 Engine | 按已声明的依赖、完成记录、节点等待和失败选取就绪节点。每个等待在原 checkpoint 内保留节点、round、owner、引用、原因和开始时间。旧单中断按存档时间读取。只有无节点可推进时，Run 才投影一个主等待点。节点效果、完成事件和 checkpoint 继续由同一事务提交。 |
| GX 恢复与项目工作视图 | 自动推进逐个检查等待。确定目标后，在取得推进权的同一写事务中复核当前引用、权限、策略和状态。显式 resume 必须给出本次等待的引用。这让用户能恢复第二个分支，也让已消费的旧引用失效。自动推进保留失败位置；操作员可通过已有 continue 接口显式重试。 |
| Driver 与计量 | 每轮先处理可本地生效的消息、持久事件、已有 Job 和确定性节点，再调用原生宿主。显式关闭 Codex 时，GX 将确实需要宿主的操作按 scope 挂起。意外断连写入待办、起始时间和重试时间，原 turn 和预留可以跨进程恢复。 |
| JobService | 只读观察已有 Job 时区分暂时读不到、真正的缺失或损坏，以及已取得的终态。观察暂缓时保留同一 Job 和预留，继续轮询同一 Run 的其余 Job。取得有效终态后由现有 JobService 保存产物并结算。 |

[Engine](gx-h/gx_core/orchestration/engine.py) · [节点等待](gx-h/gx_core/orchestration/branches.py) · [恢复事务](gx-h/gx_core/orchestration/reactor.py) · [公开 resume](gx-h/gx_core/orchestration/service.py) · [Driver](gx-h/gx_core/p0/driver.py) · [JobService](gx-h/gx_core/jobs/service.py)

## 用公开 CLI 运行离线示例

从仓库根目录执行。示例文件使用历史兼容的 `science_grid@2`。选择一个新目录会启动两项真实本地 CPU Job。带名字的 `--run-key` 使首次请求和重试指向同一个已授权项目。新版 `science_grid@3` 的兼容性另见验收报告。

```bash
state_dir=$(mktemp -d)
export GX_CODEX_OFFLINE=1
bin/gx-p0 start --state "$state_dir" --run-key offline-demo --spec examples/paired-grid.json > "$state_dir/start.json"
run_id=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$state_dir/start.json")
bin/gx-p0 run --state "$state_dir" --timeout 5
bin/gx-p0 status --state "$state_dir"
```

`run` 达到时限时返回退出码 75，输出包含 `driver=time_limit`。要查看单个 Run 的节点、原生等待、Job 及未结用量，以 `start` 返回的 Run ID 查询。下面的停止与恢复命令同样使用该 ID 和原状态目录。

```bash
bin/gx-p0 status --state "$state_dir" --run-id "$run_id"
bin/gx-p0 stop   --state "$state_dir" --run-id "$run_id"
bin/gx-p0 resume --state "$state_dir" --run-id "$run_id" --timeout 5
```

GX 意外退出后再对原目录运行 `run`。人为暂停后使用 `resume`。要继续需要模型的工作，在原有 Codex 登录和发行物可用的环境中，以未设置 `GX_CODEX_OFFLINE` 的新 Driver 打开原目录。验收用的三分支静态图在独立临时状态中通过现有 Workflow 注册，对外运行的生产工作流保留原版本绑定。[CLI](gx-h/gx_core/p0/cli.py) · [验收图](acceptance/phase2a/static_case.py) · [故障注入及进程观测](acceptance/phase2a/cross_process.py)
