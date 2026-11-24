# P2A Closure 的签收范围与恢复

## 历史失败的可追溯范围

两份历史 Phase 2A 日志各含相同的 465 个原始测试身份，均为 461 通过、4 个可选 DeepSeek SDK 用例跳过。按 UTF-8、逐行排序、末尾换行生成的身份清单 SHA-256 为 4dd9d8a43e6f2cbcab75a48a1d8a071373cea3cccdb9af547f8a35b3c7a97259。用户给出的 Closure ZIP 提到另一次运行失败 42 项，六个成员中没有该次测试 ID、失败输出、执行配置或原始 JUnit。对本地相邻 release、Git 记录、临时运行目录和归档的检索也没有找回该次清单。T03 的当次逐项复跑和历史失败原因具有不同的证据范围。保留当次每个身份和原有跳过原因；要判断各个历史失败属于环境、夹具或产品回归，仍需原始失败明细。[原冻结日志](acceptance/phase2a/regression/final-v2/batch/suite/python-full.log) · [环境复测与身份账本](P2A_TEST_ENVIRONMENT_REPORT.md)

补交历史日志后，可在同一版本执行以下命令。该命令保留原文件及其摘要，并单独生成新的判定记录。

```bash
bin/gx-acceptance run phase2a --case T03 --original-failures /path/to/original-junit.xml
```

## 官方宿主和成本

协议测试在隔离环回网络使用锁定的 Codex CLI 0.169.0 和本地脚本上游；另有真实本机 CPU Job。此次不包含新的付费模型波次、外部集群调度或独立科研审查。官方宿主可以在中断时留下完整响应的用量下界，但缺少能证明末次响应费用的记录。估算用于该笔预留和资金控制；即使估算后来等于某个观察数字，成本报告仍须依照实际归属证据分类。历史失败归档保留原判，新合同的专项与原合同可以分别复核。[用量报告](P2A_09_USAGE_RECONCILIATION_REPORT.md) · [原始中断记录](acceptance/phase2a/regression/final-v2/adjudication.json)

工作区通知与 Job 通知可在本机打断 Driver 等待。文件系统缺少相关能力或提示丢失时，Driver 依据持久状态和维护期限复查；实际提交延迟还受 CPU 调度和数据库写锁影响。事务的 p50、p95 和最大值均为该次样本统计。[空闲唤醒实测](P2A_IDLE_BACKOFF_REPORT.md) · [SQLite 竞争实测](P2A_SQLITE_TRANSACTION_TELEMETRY_REPORT.md)

## 升级与回退

1. 在原安装上让现有 Driver 退出，在副本上先运行只读 `bin/gx-p0 status --state DIR --run-id RUN`。备份完整工作区，包括 SQLite、Artifact 和 Job 侧车。仍有外部 Job 时保留原身份和结果路径；从两个目录同时接管同一 Job 会使恢复测试失去意义。
2. 新代码第一次以可写方式打开数据库时，原 schema owner 增量安装计量表。验收使用一份带 128-token HELD 预留的工作区和一份有三笔已结算 Turn 的工作区，对每份旧库各 118 张业务表比较原列的内容摘要，并再次可写打开核对全库。原事件、Job 和预留保持原始记录。[兼容性探针](acceptance/closure/legacy_ledger.py)
3. 需要回退时关闭新 Driver，保留升级后的目录与新产生的 Job/结算信息，恢复升级前整套备份并使用对应的旧版。再次执行 Job 前核对备份之后是否已经产生外部结果。旧版的操作演练包含恢复原目录、核对产物、重复打开以及实际公开 CLI 回执。[既有回退演练](PHASE_2A_ROLLBACK_REPORT.md)

本批继续使用静态 Workflow Engine、既有 Job 与预算所有者。动态 Graph、独立 Plugin Worker 和第二种外部计算后端属于后续阶段。[Closure 原始合同](GX_H_P2A_Closure_Package.zip)
