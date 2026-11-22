# GX Codex 源码宿主

在 GX-H-V2 下，`codex/codex-rs/gx-codex-host` 包含 Rust 入口、线程生命周期扩展和原始 usage 读取器。宿主直接链接同一个 workspace 的 codex-core 和 codex-app-server 库。GX 通过 stdio 复用 NativeRouter 及既有业务事务。ThreadManager 使用默认 LocalAgentControl 和 Controller ownership。线程创建、子线程启动和冷恢复都会运行 thread lifecycle contributor。Codex 核心在该回调之后读取 outcome reporter。

从仓库根目录构建 Linux 源码宿主，使用 Rust 1.95.0、C/C++ 工具链、pkg-config 和 OpenSSL 开发包。下面的命令将编译结果安装到 GX 默认查找的位置。构建源为 `61778f51d98421dd25fa35a71d9bb988c8f3973f`，兼容基线为 `d32063f96c2dff1dc96af839b3952abb6acfeda4`，tag 为 `rust-v0.169.0`。

```sh
cd codex/codex-rs
RUSTUP_TOOLCHAIN=1.95.0 CARGO_PROFILE_DEV_DEBUG=0 \
  cargo build --locked --target-dir target -p gx-codex-host
install -D -m 755 target/debug/gx-codex-host ../../bin/gx-codex-host
cd ../../gx-h
```

从 GX-H-V2/gx-h 目录运行下列命令。脚本化测试使用本机 Responses 服务、临时目录和真实 Codex 进程。

```sh
../bin/gx-codex-host --version
python3 -Bm tests.runtime_child_probe --source-host ../bin/gx-codex-host
python3 -Bm tests.runtime_interrupt_probe ../bin/gx-codex-host
python3 -Bm tests.science_runtime_comparison --source-host ../bin/gx-codex-host
```

启动使用已配置模型账户的新项目，保留当前 Clash 环境变量。

```sh
GX_RUNTIME_BACKEND=codex_source_host bin/gx-project \
  --project grid-native --state-dir "$PWD/grid-native-state" \
  --prompt-file examples/science_native/question.md \
  --max-sessions 8 --max-wall-seconds 1800
```

GX 默认查找同级 bin 下的构建，也可通过 `GX_CODEX_HOST=/absolute/path/gx-codex-host` 指定。重启同一个 state-dir 和 CODEX_HOME，会从 native-session.json 恢复后端选择和 Root。新项目默认使用 Codex App Server。历史未带 runtime_backend 的描述符按 App Server 读取。使用另一后端对照时请给它新的 state-dir。活跃 Root 收到键盘中断后会向已观察的 thread 和 turn 发 turn/interrupt。项目的后台 Job 由 GX Job 层持有。

每个收到的 terminal callback 在 state-dir/codex-outcomes 中留下按 session、thread、turn 定位的不可变文件，可与原始 rollouts 对账。写完文件并同步目录后回调返回，线程退出顺序由 Codex 管理。该文件记录 Runtime 结果，正式科研判断仍由 GX 任务提交与评估产生。投递失败时宿主尝试保存至相邻 .undelivered 目录。在恢复原目录的可写性后，正常启动会重放，或可单独执行下列第一条命令。原路径与待投递路径同时不可写时，错误记入 stderr，需从原线程核对结果。

```sh
../bin/gx-codex-host --outcome-directory /absolute/state-dir/codex-outcomes redeliver
../bin/gx-codex-host usage /absolute/codex-store/sessions/path/to/rollout.jsonl
```

usage 命令用 Codex 自身读取器读取一个或多个持久 rollout，也支持 Codex 压缩格式。每个 response ID 的实测 token 与 thread/turn 归属一并返回，重复文件不会重复计数。相同身份的数据冲突时输出不同观测并把总数置 null。观测不全时只汇总有报告的 response，费用保留 null。GX 原有 thread/tokenUsage/updated 计量提供独立的累计快照。

在使用本地 Clash 的项目环境中，Cargo、Git 和 rustup 继承已配置的代理。构建后可从 `codex/codex-rs/` 离线运行宿主测试。

```sh
RUSTUP_TOOLCHAIN=1.95.0 CARGO_PROFILE_DEV_DEBUG=0 \
  cargo test --locked --offline --target-dir target -p gx-codex-host
```

上述三个 Python 探针可分别复现子线程生命周期、中断以及两轮科研工作流。对照使用已安装的 App Server 和本地构建的源码宿主。比较时间时应记录构建类型。
