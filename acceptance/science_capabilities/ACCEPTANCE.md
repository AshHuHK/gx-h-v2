# 科研能力层验收

本版把科研问题、原件取得、阅读回执、具名计算、决定和后继研究接在现有 GX Run 上。真实模型采用部署机的官方 Codex OAuth 和现有订阅。受控原生回调、独立 CPU/GPU 复算、真实模型研究分别记证。基线为 `science/multi-workflow@d593514`。[启动与恢复](README.md) · [部署依赖及回退](MIGRATION.md)

## PI 可以从结论查到什么

| 真实研究 | 可核查的结论、原件和限制 |
|---|---|
| 海藻糖与蔗糖 | 两道独立的 S288C 证据归因题。把原文的菌株、制剂、加入的底物和测量终点，与库中 accession、反应方向及证据代码逐项对上。仅有生长表型或数据库指针时，仍留出直接催化证据的缺口。[逐篇、逐段独立复核](YEAST_REVIEW.md) |
| 已沉积 Top7 | 1QYS 中 106 个声明位置只有 92 个有完整主链。3–94 位的 Cα 回转半径为 12.216 Å，全长几何未知。独立 H800 校准只输入同一条已知序列的可比区段，92 个 Cα 的 RMSD 为 1.012 Å。GPU 对照独立于 GX 的 CPU Job。[Lead 决定](live/top7/DECISION.md) · [独立预测的输入和产物](structure_controls/README.md) |
| 已沉积 6BEN | 原件的共价闭环声明为 1，删除该声明的对照为 0。20 对模型端点距离逐一相同，均落在 1.328–1.341 Å。用端点接近判断拓扑会把对照误判为闭环。范围限于档案质控及几何量测。[Lead 决定](live/macrocycle/DECISION.md) |
| 已发表 BglB 酶学 | 100 项记录中 78 项有定量值、8 项受检出限约束、14 项为 ND；41 个留出变体中 30 个有定量值，14 个有匹配评分。后者的 Spearman ρ 为 0.259626。原登记仅含 S1，加入 S3 的结果只支持描述性比较。[Lead 决定](live/bglb/DECISION.md) · [独立复算](INDEPENDENT_CONTROLS.md) |
| 自建证据覆盖工具 | 同一字段级工具从独立材料中计算有实验论文指针的 accession 比例。分母为此次快照的不同 accession；缺指针本身没有给出酶学阴性。[父题决定](live/record-coverage/DECISION.md) · [工具输入、校准和复跑](../../examples/record-method-authoring/README.md) |

## 按蓝图签收

| 项 | 已运行和保留的边界 |
|---|---|
| H · 多轮 | 公开 CLI 区分提问与明确作答。Chromium 和临时投递故障验证同一消息恢复、网页后继实际启动。语言偏好覆盖正式报告和封存后读本。已保存两份历史真实系列的正确/错误引用复验。[多轮回执](evidence/s0-dialogue.json) · [报告语言](REPORT_LANGUAGE.md) |
| R · 检索和阅读 | Europe PMC、Crossref、arXiv 分别承担候选、出版及版本定位；模型在真实课题里自行调整查询、读取 JATS 段落和表。受控研究员可在冷启动后读另一人取得的原件；跨页 PDF 明示原件页标及文本覆盖。公众号、小红书各有链接接收和授权导入；本次在线取得正文 0 篇。[读者交接](evidence/scholarly-cold-reader.json) · [arXiv](evidence/arxiv-handoff.json) · [社交来源](PUBLIC_SOCIAL_ACCESS.md) |
| D · 数据 | 实际查询 Rhea 142、Swiss-Prot 2026_03、ENZYME 28-Jan-2026，取得 UniProt 原始 JSON，使用 BLASTp 和旧 UniProt MCP。两种底物在独立受控复验中合计 6 个成功 Job。原插件 worker 错选 Node 12 的失败保留，新版本固定部署的 Node 22.17。[海藻糖行级记录](evidence/yeast-trehalose-jobs.json) · [蔗糖行级记录](evidence/yeast-sucrose-jobs.json) |
| T · 工具 | Lead 在原 Goal 里选择字段和出处，经开发 Job 生成并校准一个可安装版本，再派发真实 CPU Job。可派生范围为标识符关联和证据覆盖；模板及反例固定，字段选择与读数解释由 Lead 负责。[同题真实方法记录](live/record-coverage/method.json) · [并发选择、禁用与恢复](CAPABILITY_VIEWS.md) |
| W · Workflow | 已成功执行的小图可保存、锁定版本、用新原件实例化，再由现有图采纳和 Job 派发。具名输出测试覆盖同一上游的多产物换序。旧 RFD4、AP.Novo 和环肽设计只登记历史范围。[配方协议与结构对照](RECIPES.md) · [旧资产清单](MIGRATION.md) |
| L · 生命周期 | 工具选择附加在同一 Goal，已接 Job 维持方法绑定。两个上一版 Run 在隔离副本重开，报告字节相同；本期真实父题迁入独立部署后，原配方、报告和决定均可恢复。三次重导增加 Job 和模型回合均为零。[历史兼容](evidence/archive-upgrade.json) · [本版冷恢复](evidence/sealed-record-recovery.json) |
| S · 证据与报告 | 每个正式 Run 的目录保存原简报、完整决定、结果 JSON、宿主回合与结题事件。正式摘要可能省略区段或分母；DECISION.md 直接呈现已采纳内容。独立研究员用归档材料复算了三个结构/酶学结果。[三项复算及分母](INDEPENDENT_CONTROLS.md) |

四道指定领域题中，A 覆盖天然酵母酶的公开证据归因和跨底物比较。B 完成固定已知序列的预测校准，C 完成已发表面板的回测，D 完成已知档案的拓扑正反对照。B 的新序列设计、C 的新变体优化、D 的新环肽生成与独立预测均未实施；这三条路线不能按原任务单整体签收。当前标准 AF3 后端也未验证所需的聚合物内部自定义闭环。所有科学结果均来自公开实测资料再分析或本次计算，本次新湿实验为零。

## 工程回归与现场发现

基线的 46 项兼容、21 项多轮、14 项集成共 81 项全过。产品提交 `0664513` 复跑这三组，并通过新增科研用例 24 项和受影响接口/恢复用例 39 项，合计 **144 项通过，0 跳过**。[分组清单、解释器与版本](evidence/test-results.json)

现场两道酵母题的 8 次原始拒绝来自同一类交接故障。GX 已把有出处的摘录回给研究者，却在当回合的决定中拒绝其证据引用。修复按同一 Run、任务、租约、回合及确切工具回执认领引用，独立保留命题类型及图输入权限。8 份原始提交的证据检查在只读回放中通过；原来未被采用的决定继续保留原判。[逐笔回放](evidence/source-citation-replay.json)

官方模型回合可证明两道酵母研究确实并行。首个可核查的跨题交叠为 34.721 秒；PI 的定向意见发生在同一海藻糖 Run 两位研究者的 20.536 秒交叠内。文献模块随后执行了一项全文获取与两项检索，决定 946 引用了对应的三个工作产物。[并发与单路线反馈回执](FIELD_COLLABORATION.md)
