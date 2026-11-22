# 本机部署与运行

在本包根目录运行 `bin/gx-p0`。本机已验证 Python 3.11.9、官方 `@openai/codex@0.169.0` 和已有的 ChatGPT OAuth 登录。启动器从本包的 `gx-h` 导入 GX，在本机通过 stdio 启动官方 App Server。模型请求和设备授权使用 Clash。一次真实 `xhigh` 请求已收到预期回复。[OAuth 运行记录及模型目录](audit/OAUTH_EFFORT.md) · [本机 doctor 结果](acceptance/runs/oauth/doctor.json)

## 本机首次运行

```bash
./bin/gx-p0 login-status
./bin/gx-p0 doctor
```

`login-status` 的 `chatgpt_oauth` 为 `true` 时可复用该账号。若显示 `signed_out` 或 `other_auth`，按下列命令打开官方设备授权流程，在终端显示的 OpenAI 页面完成验证。若该账号已经登录，`login` 直接返回。登录状态与授权更新由官方 Codex 维护，GX 项目状态可以放在独立目录。[官方认证指南](https://learn.chatgpt.com/docs/auth)

```bash
./bin/gx-p0 login
./bin/gx-p0 doctor
```

`doctor` 检查实际二进制版本、代理入口、App Server 的 `account/read` 和 `model/list`。输出 `ready: true` 表示所选模型的目录声明覆盖三个所需档位。需要重新验证上游推理可用性时，可单独运行 `./bin/gx-p0 doctor --smoke`。这会向所选模型发出一次计入账号用量的 `xhigh` 短文本请求，并核对原生 turn 终态和回答。普通 `doctor` 只读取账户与目录。

## 在另一台 Linux 主机安装相同 CLI

安装路径可放在用户目录。下面的命令复用本包的代理设置，检查本地 Clash 监听后再调用 npm。启动器对已有绕过表取允许环回地址的交集。

```bash
PYTHONPATH=gx-h python3 -B - <<'PY'
from pathlib import Path
import subprocess
from gx_core.p0.profiles import clash_env

env = clash_env()
prefix = Path.home() / ".local/gx-codex-p0"
subprocess.run([
    "npm", "--proxy", env["HTTP_PROXY"], "--https-proxy", env["HTTPS_PROXY"],
    "--registry", "https://registry.npmjs.org", "install", "--prefix", str(prefix),
    "--ignore-scripts", "--no-audit", "--no-fund", "@openai/codex@0.169.0",
], env=env, check=True)
PY
export GX_P0_CODEX="$HOME/.local/gx-codex-p0/node_modules/.bin/codex"
./bin/gx-p0 login
./bin/gx-p0 doctor
```

已有官方二进制时，`GX_P0_CODEX` 也可直接指向它；默认从 `PATH` 解析 `codex`。npm 包同时声明了对应平台的原生可选依赖，执行安装需允许 optional dependencies。正式运行的 provider 为 `openai`。本机部署启用官方 `respect_system_proxy` 并关闭 `system_proxy_fallback`。任一配置的 Clash 入口连接失败时，启动器给出地址并停止发起网络请求。[官方 CLI 发行文件核验](acceptance/runs/schema/cli-provenance.json) · [代理和角色配置](gx-h/gx_core/p0/profiles.py)

## 角色档位

默认模型为 `gpt-6.2-astra`。主协调者、组长和项目决策使用 `ultra`。推理、规划、代码开发、科研分析和独立科学审查使用 `max`。简单任务、常规执行和测试使用 `xhigh`。Codex 的 `ultra` 可以主动分派临时子任务，其普通推理请求使用模型目录给出的多代理档位。GX 的持久任务保存实际线程配置。[配置实现](gx-h/gx_core/p0/profiles.py) · [档位源码与账号实测](audit/OAUTH_EFFORT.md)

`GX_P0_TEST=1` 令所有验收角色统一使用 `xhigh`。该模式也为原生子代理设置 `xhigh` 默认值并关闭 spawn 工具上的模型/档位覆盖。离线协议夹具另设 `GX_P0_TEST_PROVIDER` 为其回环 provider 名称，并使用夹具专属 `CODEX_HOME`。指定 `GX_P0_MODEL` 可选择该 provider 的模型名称。生产 OAuth 验证使用已有的官方 Codex 登录环境。[网格回环验收](acceptance/e2e_grid.py) · [启动器回环验收](acceptance/oauth_loopback.py) · [线上传输字段的回环观测](acceptance/runs/oauth/loopback-smoke.json)

## 启动、暂停和继续

为每个项目选择单独的状态目录。内置配对网格示例是一项有计算和模型调用的验收。以下命令在当前 shell 将测试档位应用到每次启动，再将同一份状态传给各个命令。

```bash
export GX_P0_TEST=1
state_dir="$HOME/.local/state/gx-p0/grid-trial"
./bin/gx-p0 start --state "$state_dir" --demo-grid
./bin/gx-p0 run --state "$state_dir" --timeout 1800
./bin/gx-p0 status --state "$state_dir"
```

`run` 到达等待时限时返回 75，并保留现场。继续时使用原状态目录。操作员可在运行中追加输入或暂停驱动。

```bash
./bin/gx-p0 message --state "$state_dir" --supplement '补充观测已就绪。'
./bin/gx-p0 stop --state "$state_dir"
./bin/gx-p0 resume --state "$state_dir" --timeout 1800
```

业务入口还支持 `start --state DIR --spec FILE`，接受包内当前工作流的结构化项目规格。通过 `./bin/gx-p0 COMMAND --help` 查看现有命令参数。[CLI 实现](gx-h/gx_core/p0/cli.py) · [验收矩阵](acceptance/ACCEPTANCE_MATRIX.md)
