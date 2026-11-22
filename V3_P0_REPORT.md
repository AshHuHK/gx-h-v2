# P0 本地科研插件加载

P0 的交付入口是 `bin/gx-p0 plugin`。操作员审核目录后安装指定摘要的包。加载器读取固化的包字节，按确定顺序解析依赖，在各自的 Python 命名空间执行一次注册，并冻结工作流引用、工具和能力清单引用。默认安装尚未选择插件的 v2.1 项目沿用其原始工作流。

## 安装和版本语义

- 安装目录采用 `gx-h/plugins/installed/<插件 ID>/<精确版本>`。原有 `config/workflow_packages.json` 只增加安装路径和 SHA-256。包发布在目录中、目录记录写入前崩溃时，可以重跑同一条安装命令恢复。再次安装相同版本的不同内容会提示冲突。
- 包的源代码、嵌套目录、数据和 Job 脚本都参加内容摘要。宿主使用同一次读取的源码运行延迟导入，两个版本各自保留模块变量和注册结果。`job-assets/` 中的方法交给 GX 既有计算环境。
- 已安装插件在操作员显式选择时加载。失败会留下具体的依赖链，其他可用插件可以继续。注册操作限于受信本地代码；静态导入策略用于约束作者接口。

[安装命令](examples/plugins/README.md) · [导入与版本决策](V3_IMPORT_AND_VERSIONING_DECISION.md) · [加载器](gx-h/gx_core/plugins/loader.py) · [Author API](gx-h/gx_core/plugins/author_api.py)

## 验证方法

| 范围 | 入口与判据 |
|---|---|
| 清单、依赖、快照和版本共存 | `cd gx-h && ../.venv/bin/python -B -m unittest discover -s tests -p 'test_v3_plugin*.py' -v`，验证拒绝时包未执行、嵌套与延迟导入、不同版本的独立状态、同时加载仅注册一次、失败清理、安装目录与 v2.1 Run 兼容性。 |
| 安装中断、安装并发与恢复 | 在临时发行拷贝中，于包目录落盘且安装记录尚未发布时结束进程。用独立 CLI 检查可见性，按原摘要恢复，再用多个进程并发安装；核对版本数、原工作流记录和冷启动命名空间。[重现脚本](acceptance/v3/p0/operator_probe.py) |
| 完整平台回归 | 从 P0 的 Git 提交导出代码，在独立目录运行原 Python、Node、生成器和固定 Codex 环回测试；按测试名、原断言和已锁定 Codex 二进制核对。[对比脚本](acceptance/v3/compare_regression.py) |

## 回退

P0 的安装和校验命令只修改当前 GX 安装中的包目录和操作员安装清单。可以在独立部署中恢复安装清单与对应版本目录，或使用原 v2.1 安装打开旧 Run。原 v2.1 分支为 `61ed0067448658e02d181b3cb85d7f68e39b6616`。[旧项目恢复对照](acceptance/v3/staging/README.md)

## P0 签收记录

产品提交为 `177037621d935bbac9e31618ebbc095f21e613b8`，独立导出的产品树 SHA-256 为 `48a405187de71044b39216df9ec5c26e1ac34b42c6f2fc0b94ad695d369eb1eb`。

| 实测 | 结果与证据 |
|---|---|
| 原实施包示例与本版工具共存 | 同一 Python 进程执行原版 `0.1.0` 的两个工作流钩子，再执行 `0.2.0` 的问候工具。两个命名空间、内容摘要和结果已保存，退出码 0。[共存记录](acceptance/v3/p0/example-versions.json) |
| 针对性测试 | 16/16 通过。公开 CLI 的 17 条安装、拒绝和恢复命令逐项保存；工具示例 0.2.0 在安装副本中通过验证。[测试名与结果](acceptance/v3/p0/loader-tests.log) · [CLI 回执](acceptance/v3/p0/cli-staging.json) |
| 提交后的独立安装故障测试 | 23 条 CLI 操作均得到预期结果，6 个写进程同时参与；安装中断、篡改拒绝、丢失后恢复均通过。耗时 8.896 秒，退出码 0。测到的失效范围为安装进程被终止，主机和文件系统仍在线。[原始命令](acceptance/v3/p0/operator-invocation.json) · [逐项结果](acceptance/v3/p0/operator.json) |
| 完整回归 | 从 Git 导出上述提交，259.043 秒完成，退出码 0。Python 发现 360 项、通过 356 项，4 项可选 DSH 环境测试沿用基线跳过。P0 子集 124/124，Node 2/2，生成器检查通过，固定 Codex 环回 17+13 项全部通过。跨文件系统的临时目录清理提示未影响返回码。[命令与环境](acceptance/v3/p0/regression/invocation.json) · [逐项耗时和结果](acceptance/v3/p0/regression/summary.json) · [原用例与原断言核对](acceptance/v3/p0/regression/frozen-comparison.json) |
