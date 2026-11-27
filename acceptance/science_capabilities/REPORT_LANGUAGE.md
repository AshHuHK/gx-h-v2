# 封存报告与译读本

英文 Goal 经普通 `gx-p0 message` 收到“以后报告用中文。”后，Main 将原话登记为持久呈现偏好。下一份 Lead 简报给出目标语言。若当时的简报已经送达，提交结题时也会核对摘要、理由和每项局限。Lead 可以在同一回合修正用语。采用的语言和来源消息固定在生效的结题回执上。报告仍从原决定及其收到的具名量测渲染。此版本支持中文与英文。

封存后可在同一对话请求“请将这份封存报告译成英文”。Main 通过 `gx_project_state` 读取这次生效的结题决定，通过 `gx_route_pi_message` 提交摘要、理由和各项局限的译文。再次执行 `bin/gx-p0 research --state STATE --run-id RUN`，返回的 `readers` 列出独立的 `READER.*.md`，文件中可查原决定、原件和产物引用。`REPORT.md` 继续重导封存原文。原始预测和各分支历次提交保留当时的语言。

## 复现与验收

基线 `d593514` 中，Main 在测试回执 `clause_52aa6afa9fd53880ef1fcb7920c06c3e` 登记“以后报告用中文。”，随后普通导出仍出现英文摘要 “Record the known coverage at closure.”、英文理由和局限 “Sampling independence remains unknown.”。关闭决定为 `gxa_9b9a7d34c1b334d4ad92df13fdceb5c78b46e268bba53fb0a1e719966cf79fcd`，生效于事件 39。

当前测试使用真实 CPU 坐标分析 Job 与受控的 Main/Lead 回调。留存回执包含原件、Job、读数送达和结题事件、正式中文报告与封存后英文读本。测试核对两份文件的读数、单位、局限和决定归属，覆盖改写单位、数字和删除局限的反例。官方模型自动选择这项呈现能力的行为需用真实宿主单独验证。[正式报告及读本实测](report-language.json)

在产品仓库中复跑：

```bash
GX_CODEX_OFFLINE=1 PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a \
  .venv/bin/python -B -m unittest -v \
  acceptance.science_capabilities.test_report_language \
  acceptance.multiturn.test_pi_clauses \
  acceptance.multiturn.test_sealed_intent \
  acceptance.science_execution.test_research_page
```

需要保留另一份完整受控回执时，可在该命令上设置 `GX_REPORT_LANGUAGE_RECEIPT` 指向新的输出文件。
