# 旧能力迁入 GX-H

盘点来自部署机的只读索引。117 个 MCP 组件、958 个工具定义、192 条 workflow 定位及 554 个技能文件分别统计，条目有交叉。当前发布的可执行范围由安装的确切插件版本、Goal 的方法视图及实际 Job 回执决定。[旧部署定位与摘要](../../gx-h/config/research_asset_cards.json)

| 来源 | 本期接入 | 可以核查的边界 |
|---|---|---|
| 原学术检索、信息源 | Europe PMC 与 Crossref 元数据合并、PMCID 正文取得、独立 arXiv 检索及版本化 PDF | 入库保存原件、来源、版本或抓取时点、内容摘要和解析覆盖。相同 PMCID 的两次取得属于同一来源。 |
| 公众号、小红书 | 接收公开链接或授权导出，保留线索和覆盖 | 两个历史链接均受实际读取路径规则限制。链接本身可登记为零段正文，不能证明目标研究结论。[访问实测](PUBLIC_SOCIAL_ACCESS.md) |
| GX2Understand、Paper2Skill、Paper2Agent | 按需目录可查旧说明与来源摘要。原件阅读、带位置的摘取和复核使用 GX 已有读者。一次有来源的字段选择可派生成有独立对照的 CPU 记录关联工具。 | 当前作者能建立并安装“标识符关联与证据覆盖”这一类确定性分析，作者材料与本地估计规则分别标明。[跨题方法](../../examples/record-method-authoring/README.md) |
| 原生 v2 酶库及旧 UniProt MCP | 酿酒酵母 S288C、海藻糖和蔗糖查询。到只读 Rhea 142、Swiss-Prot 2026_03、ENZYME 28-Jan-2026 索引取行，以部署的 BLASTp 和真实旧 MCP 操作交叉读取。 | 在其它机器上须提供相同快照、工具与运行时。安装文件列出确切挂载与 MCP 构建。发布包带小型科研样本，整套历史库仍按原部署提供。[依赖清单](../../gx-h/plugins/installed/org.gx.yeast-enzyme-evidence/1.0.1/methods/mounts.json) |
| 已发表结构和酶学数据 | 1QYS 声明与坐标覆盖、6BEN 显式拓扑与缺键反例、BglB 发表记录留出回测。正常 Goal 可调，产物可成为后继配方的输入。 | 原始沉积、加工反例、模型输出、已有实验各自记录。[结构复算](structure_controls/README.md) · [酶学来源](../../examples/bglb-retrospective/README.md) |
| RFD4、AP.Novo、旧蛋白与环肽生成路线 | 保留历史定位及原验收口径。本版未接入自主从头设计和变体优化。 | 历史 RFD4 盘点同时包含已通过、失败和延期的配置。AP.Novo 的准备、解析和历史数值一致性是不同证据。本次路线级新增 GX 生成 Job 为 0。 |

## 升级与回退

已有部署在同一源码路径升级。关闭当前 driver 后，用 SQLite backup 和工作区的 artifacts、runs、threads、research 一并留存原状态，再切换到新发布。保留旧包文件以履行已接 Job 的版本绑定。两个上一版封存 Run 的隔离副本已从普通 research 入口在新版重开，原决定、REPORT.md 摘要完全相同，新增 0 个 Job 和模型回合。[两份重开回执](evidence/archive-upgrade.json)

回退到本次起点 d593514 时，复用升级前的部署目录与对应备份，或重新挂载保留的旧目录。另行保留新部署的 SQLite、产物、运行中采用的派生插件和其安装清单，以便恢复这批研究。若仅停用某项方法，可在相应 Goal 用 capabilities 禁用该版本，已提交 Job 继续依原版本结算。

## 在独立部署冷恢复已结题的真实配方

`restore_sealed_recipe.py` 通过目标发布的普通插件安装命令恢复研究期间生成的方法，按来源部署的目录摘要核验。源工作区停用后，脚本取得共享读锁，将 SQLite 与 WAL 暂存到私有目录，由 SQLite Backup API 创建副本。目标路径使用目标发布签发的所有者标记。原件、Job 文件与归档会话都按文件哈希比较。会话只留在本机私有目录。

在 `gx-v-6` 下执行。目标工作目录须为新的空位，重跑验收可指定另一个新位置。

```bash
science-capabilities-release/.venv/bin/python \
  product-multi-workflow/acceptance/science_capabilities/restore_sealed_recipe.py \
  --source gx-science/capabilities-check/live-record-coverage \
  --destination gx-science/capabilities-check/live-record-successor \
  --source-release science-methods-deployed \
  --release science-capabilities-release \
  --provider org.gx.record-coverage-8902880ed9d712bf6baa5899@1.0.0 \
  --recipe accession-pmid-coverage@1 \
  --run-id gxr_01m8d9xyaych3h1z2b
```

在 `956ab21` 到 `ebb5ec0` 的实测中，`gx-p0 research` 重新导出的封存报告与原报告逐字节一致。配方版本、摘要、结题决定、原有 2 个 Job 和 6 个模型回合均通过对照。重导新增 0 个 Job、0 个模型回合。历史环境记录保留来源部署的解释器路径。[冷恢复回执](evidence/sealed-record-recovery.json)
