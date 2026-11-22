# v2.1 故障注入实测

测试仅在本发布目录的独立 staging 运行。操作者通过 `bin/gx-p0`，官方 Codex 0.169.0 通过环回 Responses 服务读取数值证据并提交产品。每次运行使用独立 SQLite、artifact 目录、CODEX_HOME 和临时目录。以下所有 Job 均执行本地合成数值实验。

| 注入点 | 操作和实际结果 | 证据 |
|---|---|---|
| R0 决策后，原 Job 正在执行 | 只向脚本刚启动的 GX 公共 CLI PID 发 SIGKILL。该时刻所有已入场 native turn 都已结算，B 的后继 Job 有活跃运行状态与 backend receipt。冷开后一轮决策使用原 Job ID、幂等键及 scheduler ref，所有 Job attempt 均为 1。 | [两队短程实测](acceptance/v21/long-run/smoke.json) |
| R1 reviewer 已选定只读工具，回调尚未到 GX | 单独 CLI 先执行 stop。官方线程收到 GX 的 SCOPE_DENIED，在该 turn 正常结束。resume 启动新进程，同一线程再次请求同一 artifact、成功提交。前一轮决定引用和两个 submission ID 全等，原 lease、attempt 1 保留，失败计数为 0，三轮完成。 | [固定断点及相同读取引用](acceptance/v21/long-run/pause-read.json) |
| 已接收数值产品，官方末次回执仍在途 | stop 针对 A，聚合 Driver 等待已入场 turn 结束，A 停止阶段检出的未结 hold 为 0。B 可以继续，最后以新 CLI 恢复 A。 | [stop 及时返回与后续 drain 的完整状态](acceptance/v21/long-run/long.json) |
| 首次真实 HTTP 请求收到一次 429 | 修前有正式 quota receipt、原 turn 结算和开放的 provider wait；官方线程状态 systemError 导致超过 3 秒重置窗口后仍无法重试，100 秒驱动退出 75。修复后按官方原 failed turn 重验，同一线程在窗口届满后重新入场。3 轮、4 个 Job 成功，attempts_consumed 为 0。 | [修前](acceptance/v21/long-run/http-429-before.json) · [修后](acceptance/v21/long-run/http-429-after.json) |
| 最后一个 Run 用带 selector 的 resume 完成，然后从全新 CLI 重开 | 首次对照发现项目成立时两笔子预算到终态冷开才结转。已结算付费 turn、科学结果、Job 均未变化。修复后在 selected CLI 完成时完成结转；冷开对全部 22 张业务表逐行排序算 SHA-256，全等，新模型请求为 0。 | [修前差异](acceptance/v21/long-run/terminal-replay-before.json) · [修后](acceptance/v21/long-run/terminal-replay-after.json) |

**复现实验。** 从发布目录运行下列命令。脚本将新状态和完整 stdout、stderr、Responses 请求与计时日志保留在 `var/v21-long-run/`。每个故障都由脚本的业务断点触发。观察者使用只读 SQLite 连接，不修改租期时间戳、Job 结果、任务状态或账本。

```bash
.venv/bin/python acceptance/v21_long_run.py --rounds 3 --latency .65 --lease 8 --timeout 155
.venv/bin/python acceptance/v21_pause_read.py
.venv/bin/python acceptance/v21_http_backoff.py
```

**计量边界。** 429 没有任何用量数据，GX 用 staged 2 秒宽限期估计这一 turn。重试 turn 的官方累计报告出现后，GX 对最初估计的 2,048 单位记 −2,048 调整，原结算记录仍然存在。最后重复中，重试的付费预留发生在已记录的 provider 窗口重置后 1.310 秒。合成服务每个成功的 Responses 请求报告 35 token。官方 App Server、GX 等待/计费、租约、数据库和本地数值 Job 实际执行。没有外部 OAuth 用户、真实商用配额或外部模型费用。

**补充边界。** 在暂停读取用例开发中，一个把旧 SCOPE_DENIED 永久当作致命错误的脚本模型使后续请求返回 400。短租期过期后还触发旧的 Codex role intent 检查。正式暂停回归让模型识别既有失败收据并重新读取，验证恢复授权；旧角色检查另由对应路径的过期恢复测试验证。真实计量丢包、原生子代理未结回执、账户窗口和冷进程恢复另见[官方协议探针](acceptance/v21/protocol/)。

第一份冻结候选 `281b04c27b1aa4b72a7c18a4ffdebb86e7386835` 使用独立 Git 归档重做了公开双 Run 崩溃、冷恢复、主动停止和终态重开。两队都完成 3 轮，8 个 Job 无重复，20.265 秒后出现下一项恢复后的决策。末次重开 22 张业务表完整摘要一致，零新增模型调用。[冻结提交故障实测](acceptance/v21/long-run/frozen.json)

发布提交 `a05dcef64522fbbbc12120b1d78da518e6886b10` 在新 Git 归档和独立状态下重做上述双队流程，用时 **80.544 秒**。终止 GX CLI 后 17.720 秒出现 B 的下一项科学决策；A 的主动停止返回时有一笔在途，随后自行完成计量。两队最终各完成三轮，8 个真实 Job 的身份与后端收据无重复、失败 Attempt 为 0。再次冷开返回时 22 张业务表完整摘要全等，新增模型请求为 0。[正式发布运行](acceptance/v21/long-run/released.json) · [长测 staging 至正式发布的源码差异](acceptance/v21/long-run/stage-diff-release.json)
