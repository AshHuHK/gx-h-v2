# 将已运行的记录覆盖率方法移至新环境

这里保存了父题 `gxr_01m8d9xyaych3h1z2b` 发布的两个产物。五文件插件计算指定队列中有文献指针的唯一标识符数。配方定义同时记录原方法来源、字段映射、依赖、输入类型、单位及缺失处理方式。导出时只读原工作区，插件与配方均保留原摘要。[来源与原 Job 回执标识](source.json)

| 对象 | 固定身份 | SHA-256 |
|---|---|---|
| 安装包 | `org.gx.record-coverage-8902880ed9d712bf6baa5899@1.0.0` | `b4bcef57d78bf4ed151427e647f95394a8e8314c77b1e1c1998fdfe55a92dd2d` |
| 可执行方法 | `record-coverage-8902880ed9d712bf6baa5899@1` | `cf45433b267480075c90d0ff8a5d24eee8d0c79c9e514a6a2a5529bdd5e53981` |
| 配方原件 | `accession-pmid-coverage@1` | `74d6d077301821523c0178e9ef5d2ae4ff03871f19ced4eec3c5bd3ee0cf7a58` |

## 使用已生成的分析方法

在目标发布目录运行以下命令，指定本目录在该机器上的位置。标准安装命令会核对摘要并登记插件。然后在新 Goal 的 `plugins` 中选择上表的安装包，在 `capabilities` 中选择上表的可执行方法，即可对该 Goal 自己的材料派工。两个输入依次为含 `candidates[].accession` 的队列表和含 `pointers[].entry`、`pointers[].pmid` 的证据表。结果位于具名产物 `coverage.json`。

```bash
bundle=acceptance/science_capabilities/portable_method
.venv/bin/python -B bin/gx-p0 plugin install "$bundle/plugin" \
  --sha256 b4bcef57d78bf4ed151427e647f95394a8e8314c77b1e1c1998fdfe55a92dd2d
.venv/bin/python -B bin/gx-p0 plugin verify org.gx.record-coverage-8902880ed9d712bf6baa5899@1.0.0
sha256sum "$bundle/accession-pmid-coverage@1.json"
```

## 配方在全新工作区的行为

`a517af4` 的 `gx_instantiate_recipe` 从当前工作区的已保存配方表读取精确版本。原件 JSON 可用于检查方法与重建步骤；该版尚无把独立 JSON 直接导入空目录的正式命令。跨题直接运行已有分析方法，只需安装插件。若还要使完整配方可由新工作区的 Lead 从目录发现，可以在同一 Goal 中另加随发布提供的 `org.gx.record-method-builder@1.0.0` 和 `derive-record-coverage@1`，按原件中的两个步骤运行，再用 `gx_save_recipe` 保存这次实际完成的子图。[配方原件](accession-pmid-coverage@1.json)

实测从候选提交的临时干净副本及空 SQLite 工作区开始，使用正常插件安装、受控 Lead 工具回调和真实 CPU Job。最初查询该配方无法找到记录。两个 Job 成功后，以新 Run 的节点重新发布，定义的全部 2,457 个字节与原件一致，目录查询成功。蔗糖快照中的 1 个唯一队列标识符有 0 个具非空 PMID 的匹配，直接计数同为 0/1。该数描述所供文件的文献指针覆盖率。验收过程没有载入旧库、旧 Job 文件或 Codex 会话。[干净安装回执](clean-install.json)

从源码仓库可重跑整个独立试验。脚本只在临时目录展开固定提交并安装插件。

```bash
.venv/bin/python -B acceptance/science_capabilities/portable_method/verify.py \
  --receipt /tmp/gx-clean-install.json
```
