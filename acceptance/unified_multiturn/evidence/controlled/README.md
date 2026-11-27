# A1–A5 受控验收

行键修复前的固定候选 `82a087ed34cf8dae2b342a48e397b5de07fdbff9` 在独立工作树通过三组复验，共 81 项。兼容性 46 项用时 38.777 秒，多轮科研语义 21 项用时 49.387 秒，网页、消息恢复、后继与停滞集成 14 项用时 25.218 秒。各案例自己的 Run、事件和 CPU 产物引用来自开发期间保留的首次受控场景。固定候选使用同一测试合同、新建状态复跑。[各组模块及回执](observations.json)

首次提交 `e2a102a` 的 46 项兼容对照通过。多轮套件中，顶层 Lead 已实际收到并引用 A 的模块报告，但反馈状态查询遗漏 `module_report_ref`；恢复套件另有一项未能完成繁忙时入队消息的冷恢复。两个原断言在中间候选 `0e694e4` 和行键修复前的固定候选 `82a087e` 上均通过。另有独立的真实 Chromium 页面彩排固定在 `e2a102a`；下表分别列出浏览器观察和受控模型答复。

| 范围 | 已证实的行为 | 对应复跑模块 |
| --- | --- | --- |
| A1 网页、CLI 与投递 | Chromium、CLI 与重启后的网页显示同一组 4 条消息。SSE 以 29 为恢复游标取得后继事件 30，跨 Run 或改原文重用 ID 返回 HTTP 409。独立脚本宿主在首回合已获接收后重启，原锚点对应 2 次投递、2 个答复；Run 仍为 PAUSED，新增科研 Job 为 0。 | `test_web_science`、`test_conversation_delivery`、真实页面脚本 `browser_smoke.cjs` |
| A2 范围隔离 | 仅修改呈现语言后，原 Lead 和两个模块的任务 ID、状态不变。包含 4 部分的 PI 消息只将相应线索转给目标；重发复用回执。模块私有原件、发布后的报告和公开原件按各自范围投递。定向反馈 50 → 报告 81 → 处置 82 可从原消息追溯。 | `test_pi_clauses`、`test_pi_materials`、`test_pi_questions` |
| A3 双向澄清 | 在两个问题同时等待时，问题 A 于事件 54 提出，事件 90 回答；同键重放仍指向 90，另答得到 ALREADY_ANSWERED。A 有一个后续任务，B 的既有任务 ID 和结果保留，旧 Goal 的晚答显示 STALE。另一案例中模块报告事件 63 返回顶层，Lead 在 74 据此维持原判断，75 记录处置。 | `test_pi_questions`、`test_conversation_science`、`test_main_replacement` |
| A4 停滞与方法 | 连续一轮无进展后，原路线实际执行 PCNA 测量，1VYM 第一对亚基有 33 个接触残基对。可委派、等待、显式收敛、封存；冷恢复保留显式收敛。硬资源上限拒绝产生新 Job。方法调研的另一 Run 在事件 83 收到同一读数；DSSP/Wilke 有文献来源，目录匹配与方法 Job 均为 0。 | `test_advisory_rounds`、`test_multiturn_method_survey`、`test_rounds` |
| A5 封存与后继 | 父题在事件 61 封存。并发请求、回执前中断与同键重试得到同一后继，子题实际 CPU 结果用于事件 126 结题；父报告重导新增 0 Job、0 宿主回合。原已错位的旧库表头为版本 2，仍按封存版本 1 和原决定重导出相同报告字节。 | `test_successor`、`test_sealed_intent` |

## 复验

从产品根目录运行，下例依序复现固定候选的三组套件。Chromium 独立彩排和实际科研启动步骤见 PI 手册。[网页与科研入口](../../README.md)

```bash
export GX_CODEX_OFFLINE=1 PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a
.venv/bin/python -B - <<'PY'
from pathlib import Path
import json, subprocess, sys
p = Path("acceptance/unified_multiturn/evidence/controlled/observations.json")
for suite in json.loads(p.read_text())["pinned_rerun"]:
    subprocess.run([sys.executable, "-B", "-m", "unittest", "-v", *suite["modules"]], check=True)
PY
```

## 两份已发表研究档案

原部署 `e7da884` 的两个封存档案在候选 `0e694e4` 重新通过原始档案检查。首航有 13 个模型回合、14 个 Job、22 份具名输出，关闭事件为 641。开放题有 9 个模型回合、21 个 Job、29 份具名输出，关闭事件为 664。两份旧回执的重导增量均为 0 Job、0 宿主回合。当前构建的开放科研和在途中 PI 干预需要各自的正式实跑回执。[首航](../../../multi_workflow/evidence/first-flight/SCIENCE.md) · [开放题](../../../multi_workflow/evidence/open-question/SCIENCE.md)

```bash
.venv/bin/python -B acceptance/multi_workflow/verify_published.py acceptance/multi_workflow/evidence/first-flight
.venv/bin/python -B acceptance/multi_workflow/verify_published.py acceptance/multi_workflow/evidence/open-question
```
