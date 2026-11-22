# ACC-1 独立 Linux 发布附件

附件包含实际 GX 候选源码和测试、Codex 锁定源码、源码中的 LICENSE 和 NOTICE、全部 1,430 份实验协议文件、官方 Linux x86_64 原生发行包，以及 Cordis 和坐标复算所需的离线依赖。历史迁移夹具包含原始 GX 提交。开发机上的 sibling checkout、npm 缓存和 Codex 登录均可缺省。[依赖与上游版本锁](release-assets/lock.json) · [历史迁移夹具](gx-h/tests/fixtures/README.md)

## 从附件部署

准备 Linux x86_64、CPython 3.11（含 venv、SQLite FTS5 和 tarfile data filter）、Git、Node 22.17 或兼容的更新版本及 npm。本次验收的测试平台为 CPython 3.11.9 和 Node 22.17.0。带原生客户端的项目和回环协议测试均会检查 GX 规定的本机 Clash 端口 7890 和 7891。源码、协议文件、Node 依赖及 Python wheel 都由附件提供。

将 tar 和同名的 `.sha256` 放在同一目录，解压到新的空目录。保留 tar 中的可执行位与符号链接。

```bash
sha256sum -c gx-codex-p0-acc1-linux-x86_64.tar.sha256
mkdir gx-codex-acc1
tar -xf gx-codex-p0-acc1-linux-x86_64.tar -C gx-codex-acc1
cd gx-codex-acc1
python3 -B scripts/release_assets.py all
./bin/gx-p0 --help
```

运行随附的 2RE9 项目还需要给启动 GX 的 Python 安装独立复算库。在解包目录创建本机虚拟环境，后续各个终端使用此环境启动、运行和恢复项目。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip --isolated --disable-pip-version-check install --no-index \
  --find-links release-assets/python --require-hashes -r release-assets/python/requirements.lock
.venv/bin/python bin/gx-p0 login-status
.venv/bin/python bin/gx-p0 start --state ./var/tl1a-study --tl1a-geometry
.venv/bin/python bin/gx-p0 run --state ./var/tl1a-study
```

安装器校验上游源码归档中的 Git commit、10,235 个文件和链接、源码许可、Schema 逐文件清单、npm 发行包的 SHA-512 与原生可执行文件的 SHA-256。完整 Linux npm 包保留官方辅助程序和各组件许可证。GX 可直接使用已安装的官方客户端。有明确部署需要时，使用 `GX_P0_CODEX` 指定相同版本的可执行文件。[Codex 许可证](release-assets/licenses/codex/LICENSE) · [上游 NOTICE](release-assets/licenses/codex/NOTICE) · [协议文件摘要](acceptance/runs/schema/schema.sha256) · [项目启动、授权与恢复](DEPLOYMENT.md)

## 一次离线复验

从解压后的目录运行以下命令。输出目录必须尚未存在。验证会安装附件中的资产，在临时空 venv 内从 wheel 安装 Biopython 1.88 和 NumPy 1.26.4，用同一份候选代码运行迁移、回退、Cordis、真实坐标 CPU 计算和独立数值复算。官方 App Server 使用回环 Responses 服务验证握手、冷恢复及公开 CLI 的三轮合成项目。每条命令保留退出码和日志。正式 OAuth 科研结果单独使用项目自己的验收记录。

```bash
python3 -B scripts/smoke_release.py --archive ../gx-codex-p0-acc1-linux-x86_64.tar \
  --output ../acc1-release-check
```

Cordis 可单独安装和验证。npm 在一个临时空缓存中使用随附 tarball 运行 `ci --offline`。四个依赖的版本和 SHA-512 由现有 npm lock 给出。

```bash
python3 -B scripts/release_assets.py cordis
(cd gx-h && python3 -B -m unittest -v tests.test_plugin_evaluator)
(cd gx-h && node --experimental-strip-types --test runtime/plugin-host/host.test.ts)
```

完整 Python 回归从 `gx-h` 运行 `python3 -B -m unittest discover -v -s tests`。可选 DSH 使用独立 SDK 和 runtime-bin，历史运行锁定两者均为 0.2.2rc2。默认干净环境中两项 DSH SDK 测试与两项集成测试显示 skipped。单独复核可运行 `python3 -B -m unittest -v tests.test_dsh_activity tests.test_dsh_runtime`。[DSH 安装与运行说明](gx-h/runtime/dsh-sdk/README.md)

单独复验运行中项目的就地升级可执行 `python3 -B acceptance/acc1_upgrade.py --out ../p0-upgrade.json`。夹具包含原 P0 提交 `47165af` 的 `bin` 和 `gx-h` 两棵原始子树。用旧入口暂停有在途 CPU Job 的项目，换成当前发布的实际文件，在同一项目状态和官方线程上完成后续工作。[前一代 P0 运行时摘要](release-assets/lock.json) · [升级断言](acceptance/acc1_upgrade.py)

## 科学数据与独立复算

原始 2RE9 实验坐标、来源及许可、冻结的数值判据、CPU 工具以及独立数值实现均在发布树内。独立复算所需 CPython 3.11 manylinux x86_64 wheels 可在空 venv 中安装。Biopython 使用缓存中的原始 PyPI wheel。NumPy 的 915 个原始 manylinux wheel 条目按 RECORD 核对后重新封装成确定性的 ZIP，保留 RECORD、方法实现及底层库的原始字节。[坐标数据来源](gx-h/adapters/scientific/tl1a_geometry/data/provenance.json) · [冻结判据](gx-h/adapters/scientific/tl1a_geometry/data/criteria.json) · [独立参考](gx-h/adapters/scientific/tl1a_geometry/data/reference.json) · [wheel 摘要锁](release-assets/python/requirements.lock)

```bash
python3 -m venv .venv
.venv/bin/python -m pip --isolated --disable-pip-version-check install --no-index \
  --find-links release-assets/python --require-hashes -r release-assets/python/requirements.lock
.venv/bin/python -B acceptance/acc1_coordinate_method.py --output ../coordinate-method.json
```

## 从发布源码制作附件

在项目 Git checkout 中运行打包器。它读取当前源码、测试、报告、锁定资产和可执行位，按 Git 基线和实际发布字节记录清单。修复尚未提交时，清单中的 `git_base` 表示父版本，`candidate_tree_sha256` 标识实际候选。tar 的所有者、时间、顺序和安装权限均已规范化。相同输入和 Git 基线会生成相同附件。最终 SHA-256 位于 tar 同目录的 `.sha256`。

```bash
python3 -B scripts/build_release.py --output dist/gx-codex-p0-acc1-linux-x86_64.tar
```

从独立 tar 解出的源码也能用同一命令重建。打包器此时读取 `RELEASE_CONTENTS.json`，逐文件核对实际内容与模式后写出附件。`smoke_release.py --archive …` 会比较这次重建与传入 tar 的 SHA-256，并用附件内的官方客户端重新生成全部实验 Schema。

打包器可从随附文件或本地 npm 内容缓存读取官方 Codex `0.169.0-linux-x64`。已有该 npm tarball 时可显式传入 `--codex-archive /path/to/codex-0.169.0-linux-x64.tgz`。安装器与打包器都检验 registry SHA-512，原始发行件的 URL、SHA-512、SHA-256 和 Linux 可执行文件摘要均在锁文件内。Linux x86_64 以外的平台需要对应平台的发行件与验证记录。[发行件锁](release-assets/lock.json) · [原始 registry 核验记录](acceptance/runs/schema/cli-provenance.json)

维护人可以从锁定的 Codex Git 对象重新导出源码资产。以下命令使用本地 Git 和 GNU tar，在新的临时目录重建两个压缩归档，最后与随附归档比较摘要。

```bash
asset_check=$(mktemp -d)
git -C vendor/codex -c tar.umask=0022 archive --format=tar d32063f96c2dff1dc96af839b3952abb6acfeda4 \
  | gzip -n -6 > "$asset_check/codex-rust-v0.169.0.tar.gz"
tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner --mode='u=rwX,go=rX' \
  --format=gnu -cf - -C generated/codex-schema . \
  | gzip -n -6 > "$asset_check/codex-schema-v0.169.0-experimental.tar.gz"
sha256sum "$asset_check"/*.tar.gz release-assets/*.tar.gz
```

上游 Git 对象由 `scripts/fetch_codex_source.sh` 按锁定版本取得。已解压的独立源码目录含全部对应文件，重新导出 Git 格式归档需要指向含原对象的仓库。[源码获取脚本](scripts/fetch_codex_source.sh) · [数值依赖 wheel 的 RECORD 复核与重新封装](scripts/pack_review_wheel.py)
