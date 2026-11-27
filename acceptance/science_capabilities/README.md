# 从科研问题启动 GX

这次发布给 Lead 四类可组合的能力。它可以自行检索论文、把原件交给另一位研究员、查询天然酵母糖水解记录，以及把一次已经执行的分析保存给下一题。公开蛋白质控、已发表酶学回测和派生工具都有可以直接运行的 Goal。

## 开始或恢复

在部署目录运行下列命令。当前机器使用既有官方 Codex 登录，网络经本机 Clash。命令用私有目录存放宿主会话，GX 从普通 research 入口建题并持续推进。需要本地酶库的示例依赖部署机的只读挂载。[发布依赖](../../RELEASE.md) · [能力迁移及挂载](MIGRATION.md)

```bash
gx_work="$(mktemp -d -p /tmp gx-yeast-research.XXXXXXXX)"
.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$gx_work" research \
  --state "$gx_work/state" --run-key yeast-trehalose \
  --goal-file examples/yeast-sugar-evidence/trehalose.md \
  --goal-config examples/yeast-sugar-evidence/goal.json
```

重启后用上述同一目录和 run-key，或执行下面的命令并指定已有 Run ID。结题返回 REPORT.md、一份完整的 DECISION.md 和包含具名输出与送达事件的 result.json。

```bash
.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$gx_work" research \
  --state "$gx_work/state" --run-id "$gx_run"
```

| 要回答的问题 | 普通入口的材料与配置 |
|---|---|
| 蔗糖水解证据是否具有底物和菌株特异性 | 同一酵母目录的 [蔗糖问题](../../examples/yeast-sugar-evidence/sucrose.md) 及 [Goal 配置](../../examples/yeast-sugar-evidence/goal.json) |
| 结构库覆盖哪些位置，几何是否能替代拓扑 | [Top7](../../examples/deposited-structure-controls/top7.md) 和 [6BEN](../../examples/deposited-structure-controls/macrocycle.md)，各配同名 JSON |
| 发表的评分在未参与比较的位点上表现如何 | [BglB 的两份实验原表与启动命令](../../examples/bglb-retrospective/README.md) |
| 同题创建新计量，再换底物复用已保存图 | [两道连续研究题、准入范围、材料与启动命令](../../examples/record-method-authoring/README.md) |

## 发现、调用与留存

Lead 用 gx_query_catalog 按需检索 research、database、method、skill、module 或 recipe。选中方法后，GX 绑定版本、原件与具名输出，并把下游任务实际使用的文件及其 SHA-256 写入 Job。旧知识卡标为 reference_only。可执行方法由正常 Job 返回结果，故障、零命中、缺少正文和实验阴性各有不同含义。[能力视图](CAPABILITY_VIEWS.md) · [保存与调用配方](RECIPES.md)

研究员可以从 Europe PMC 和 Crossref 查找同一问题的候选，从获选记录取得出版方 JATS；也能通过 arXiv 获取指定版本的 PDF。阅读回执指向实际段落、表或物理页。库记录可按 JSON Pointer 核查。工具作者当前支持从引用材料与字段选择派生记录关联和证据覆盖方法，校准覆盖重复键、空值、零值和空分母。[JATS 跨研究员冷恢复](evidence/scholarly-cold-reader.json) · [arXiv 原件](evidence/arxiv-handoff.json) · [PDF 页覆盖](evidence/pdf-source-reading.json)

要在另一台机器上使用本次 Lead 生成的证据覆盖工具，可直接安装随包的五文件插件。新工作区也可用自己的输入重建已执行的两步配方。独立空库实算得到蔗糖 0/1，重新发布的配方与父题原件逐字节一致。[插件、配方与安装命令](portable_method/README.md)

## 复验

从仓库根目录执行以下命令。它复跑基线三组、受影响的发布与恢复契约以及新增科研用例，写入逐组日志和 JSON。真实模型研究和独立复算分别见验收报告。

```bash
.venv/bin/python -B acceptance/science_capabilities/verify_release.py /tmp/gx-science-check
```

[本次科研与工程验收](ACCEPTANCE.md) · [结构与酶学独立复算](INDEPENDENT_CONTROLS.md) · [旧能力映射及回退](MIGRATION.md)
