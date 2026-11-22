# GX-H v3 P1 独立部署快照

本附件交付已签收的 P0 插件安装器及 P1 数值网格插件的可选部署快照。产品提交为 `2476f2bdd74b53e3833ce35d08287120e4fdf0c8`。干净 Linux 环境中的离线安装、公开插件核验、新建 Run 和旧 Run 状态读取均通过。P1 性能门槛另行签收，P2 和 P3 各自受前序阶段门槛约束。[阶段状态](V3_ACCEPTANCE_REPORT.md) · [验收命令与退出码](acceptance/v3/p1/standalone-deployment.json)

| 交付物 | 实测值 |
|---|---|
| [独立 Linux x86_64 tar](dist/gx-h-v3-p1-linux-x86_64.tar) | 234,229,760 字节 |
| [tar 校验文件](dist/gx-h-v3-p1-linux-x86_64.tar.sha256) | SHA-256 `d836e34d131cac4dfc3616950255d5fd4cd3c792da50dbf5c83664cf48ba7295` |
| 归档候选文件树 | SHA-256 `b953a3dbb5c2a7977688f080d7fe86af083285784901332b162c81ed8ee4382a` |
| tar 内的 `RELEASE_CONTENTS.json` | SHA-256 `314ec4f3dafe1479fbc19f6b381cb1c48ce06042bc1adaa0e0c260e916c4cd49` |

打包使用无附加文件的 detached checkout，包含 1,668 项 Git 跟踪文件、锁定的官方 Linux npm 归档及生成的发布清单。逐项复核了解压内容的字节、符号链接及文件模式；从独立解压目录按清单重建，tar SHA-256 相同。部署、回退和限制报告随附件单独交付，可按此处的摘要关联对应产品。部署所需的软件、原生客户端与离线依赖都在 tar 内。[构建程序](scripts/build_release.py) · [文件与重建回执](acceptance/v3/p1/standalone-deployment.json)

## 离线安装

在 Linux x86_64 上使用含 venv、SQLite FTS5 和 tarfile data filter 的 CPython 3.11，并准备 Node 及 npm。实测版本为 CPython 3.11.9、SQLite 3.45.3、Node 22.17.0、npm 10.9.2。把 tar 与同名 `.sha256` 放在同一个目录，从该目录运行以下命令。`mkdir` 创建新的独立安装目录。

```sh
sha256sum -c gx-h-v3-p1-linux-x86_64.tar.sha256
mkdir gx-h-v3-p1
tar -xf gx-h-v3-p1-linux-x86_64.tar -C gx-h-v3-p1
cd gx-h-v3-p1
python3.11 -m venv .venv
. .venv/bin/activate
python -B scripts/release_assets.py all
python -m pip --isolated --disable-pip-version-check install --no-index \
  --find-links release-assets/python --require-hashes -r release-assets/python/requirements.lock
python -m pip check
vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex --version
bin/gx-p0 plugin digest gx-h/plugins/installed/org.gx.science-grid/2.0.0
bin/gx-p0 plugin verify org.gx.science-grid@2.0.0
```

安装器核验了 10,235 个上游源码文件和链接、1,430 个实验协议文件及 4 个 Cordis 依赖。Python 环境读取到 Biopython 1.88 和 NumPy 1.26.4。官方程序返回 `codex-cli 0.169.0`，其 SHA-256 为 `b452ca20f838a8855a88a41050ee4a694033fff4ea3ecdad5633e6e0e8d13f2b`。公开 `digest` 和 `verify` 返回同一包摘要 `171e6f12272b3d6cd3a8ff2055c9e092c2512113bfdc0684da48a834a6c8d0ef`。实际从包加载的贡献为 `science_grid@2`、`gx_grid_measurement` 及 `fixture-grid@1`。[上游锁](release-assets/lock.json) · [核验输出](acceptance/v3/p1/standalone-deployment.json)

## 在独立状态中启动

在上述安装目录中继续。下例为这个部署单独保存 GX 状态和 Codex 会话。新终端继续使用同一安装路径、虚拟环境、项目目录及 `CODEX_HOME`。

```sh
install -d -m 700 var/p1-demo/codex
export CODEX_HOME="$PWD/var/p1-demo/codex"
python - <<'PY'
import json
from pathlib import Path

src = Path("examples/paired-grid.json")
spec = json.loads(src.read_text())
spec["plugins"] = ["org.gx.science-grid@2.0.0"]
Path("var/p1-demo/grid-plugin.json").write_text(json.dumps(spec))
PY
bin/gx-p0 start --state var/p1-demo/project --run-key paired --spec var/p1-demo/grid-plugin.json
bin/gx-p0 status --state var/p1-demo/project
```

在新状态实测，带键的公开启动登记了两个本地数值 Job，并将插件/API、原图及方法摘要写入 Run。该 Run 进入 `WAITING_EXTERNAL_JOB`，公开 `stop` 后为 `PAUSED`。本次部署检查的模型调用为零。正式模型执行使用操作员自己的官方 Codex 登录；HTTP/HTTPS 经本机 Clash 7890，SOCKS5 经 7891。就绪后，在同一终端对活动项目运行 `bin/gx-p0 run --state var/p1-demo/project`，已暂停的项目用 `resume` 继续。[新 Run 回执](acceptance/v3/p1/standalone-deployment.json) · [登录和项目操作](DEPLOYMENT.md) · [回退步骤](V3_ROLLBACK_REPORT.md)
