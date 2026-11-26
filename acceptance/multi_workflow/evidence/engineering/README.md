# 多 workflow 工程验收回执

本目录收录已有验证。四种端点对照和双 CPU 实验使用脚本化回调与实际本地 Job，报告渲染使用已结彩排的副本。以下工程通过项与真实模型的科研结论、生产回合重叠分别计数。

[各项原始计数、事件与比较](control-receipts.json) · [双 CPU 实验原始回执](cpu-barrier.json) · [原始文件 SHA-256 与相对路径](source-sha256.txt)

## A · Lead 决定与时序

固定基线 `b8a238db01e63d941fbf0ed09a321fdf75664cbe` 的七条断言 **4 绿、3 红**。同一份合同在修复后 **7 绿**。附件与仓内用例的 SHA-256 均为 `5e2bd1326c0f15a57eb99f59b87f5f0a24af045d2dcc7af80512a2dd474b7556`。三处原错误均为 Lead 失去当前 task 的绑定。

| 原先出错的情形 | 修复后同一合同的观察 |
|---|---|
| A 私写后冷开 SQLite | 同一父 Lead 可在同轮开 B，并保留 A 的节点 |
| Lead 登记预测后 A 私写 | 原 Lead 可采纳，重复回调返回同一回执 |
| 反证报告早醒后发生兄弟写入 | 原已醒 Lead 可使用该报告作出决定 |

其余四项始终通过，覆盖无干扰登记、开放模块阻止已准备的关闭、过时修订保护子节点，以及冷开并发重放避免重复派发。权限、可见性与端点时序回归各过 43、31、32 项，套件有重叠。原日志为 `A/lead-contract-baseline.log`、`A/final-integration-lead-contract.log`、`A/authority-final.log`、`A/final-visibility-opening.log` 和 `A/final-prediction-chronology.log`。

## B · 双 CPU 与中断恢复

初次报告器读取不存在的列。修正报告器后，在同一失败 Run 读出两个 worker 的亲和集均为 `[0]`，触发共享 CPU 断言。原日志为 `B/barrier-first.log` 和 `B/barrier-original-affinity.log`。

通过的 Run 为 `gxr_01m8cfq9q0pez4xrx4`。两个单线程 worker 都到达 barrier 后才开始计算，亲和集分别为 `[2]`、`[4]`，计算窗口重叠 424,117,964 ns。两进程 CPU 时间之和减去包住两段计算的墙钟时长，得到至少 253,008,045 ns 实际双 CPU 并用时间。协调器被 SIGKILL 时两节点为 SUCCEEDED、RUNNING。同一 Run 的 `resume` 后均为 SUCCEEDED，先完成的 Job ID 和结果引用一致，两个 Job 各尝试一次。

另一次限时重开前后的两笔 Job、两个节点、两笔 attempt 和模型回合逐项相同，Job 与模型增量均为 0。该探针在 2 秒时以 `time_limit`（退出码 75）返回。原件为 `B/barrier-accepted/observed.json` 和 `B/barrier-accepted/warm-reopen.json`。本地 Job 与单 CPU 回归分别过 27、3 项。

角色与交接控制另有目录回归 8 项、模块与交接回归 23 项。脚本化接力中第一模块发布 49/164 的读数，第二模块收到该报告和产物引用后用另一遮罩补测，得到 164/164。两笔 Job、前后模块和实收引用见 `B/handoff-final/handoff.json` 在随包回执中的投影。

## C · 原件端点与缺测

五组阈值与缺测单元测试通过。用两份旧原件、两种遮罩、两个指标做的八组校准中，64 个可评价段的五类计数为 quiet 49、near_zero 3、attenuated 4、opposed 4、represented 4。另有 12 个不合格段。2RE9 骨架角度 101–120 段的 A–C 变化为 +0.61272263416704°，中位数变化为 −0.42981375108577°。原日志为 `C/signed-five-green.log`、`C/historical-calibration.log`，逐段来源为 `C/historical-calibration.json`。

四个独立合成 Run 通过普通入口注册 `deposited-block-signed@1`、绑定角色 `structure`、派实际 Job、送达、复评并关闭。共同预测为 `missed_present == 1`，参数为共同 Cα、旋角、e = 0.30°、z = 0.10°、k = 3。随包回执保存目标与实测原件各自的引用及 SHA-256、整数读取结果和最终决定。

| 合成输入 | 端点值 | 可评价 / 合格段 | GX 对目标的判断 | 登记 → Job 提交 → 关闭事件 |
|---|---:|---:|---|---|
| 局部形变 | 1 | 5/5 | supported | 28 → 37 → 63 |
| 刚体变换 | 0 | 5/5 | opposed | 28 → 37 → 63 |
| 退化几何 | null | 0/5 | undetermined，关闭引用可评价段数 0 | 28 → 37 → 62 |
| 在另一原件上测得阳性 | 1 | 5/5 | undetermined，目标无匹配读数 | 30 → 39 → 64 |

首次集成把缺测端点的首次可用事件写为 45。修复后的集成套件 27/27。旧细表的独立控制中，两次都在事件 44 完成 Job、55 登记。可推出端点的旧表在 44 已知，归入回顾性。全缺测表的首次可用事件为 null。原日志为 `integrated-targets.log`、`integrated-targets-02.log` 和 `C/e2e/with-legacy-table-final.log`。四个关闭及重开使用 `D/controls` 的同一组 Run 与 `D/D-verification.json` 对齐。

## D · 普通入口与决定理由渲染

旧入口拒绝 `research` 子命令（退出码 2）。普通入口集成 20/20。上表四个 Run 关闭后用同一 key 重开，Job 和宿主回合增量均为 0，事件数依次保持 64、64、63、65。原日志为 `D/entry-baseline-red.log`、`D/entry-integration.log`。

已结彩排 `gxr_01m8cftps3hcwzxvz3` 的 Goal v1 在事件 404 采纳关闭决定、406 关闭。该决定引用 29 个已收端点，理由中用了 5 项模板。修复前报告 3 处段落共残留 15 个占位符。修复后余 0 个，3 段都与按已送达指标代入后的理由逐字一致。原生采纳理由的 SHA-256 前后一致。随包数据保留这 5 个 display、单位、未舍入值、源 artifact 和送达事件，以便逐项核对。

迁移副本的两次 `bin/gx-p0 research` 均返回 `export_closed`。两次前后均为 11 个 Job、8 个宿主回合，九张表内容摘要保持一致。交接投影仍保留两份原始交接。Markdown 中的模板数为 0。原回执为 `D/metric-render/{baseline,candidate}/verification.json`、`D/metric-render/cli-recovery-02/verification.json` 和 `D/route-metric-cold-projection.json`。终版渲染用例 5/5，相关回归 19/19。

## 离线核对

`control-receipts.json` 中的来源名和 SHA 清单都以原归档 `gx-science/validation` 为根。携出仓库后，可直接读取随包的七项状态对、原件身份、CPU 时间、关闭事件和重开前后计数。有原归档时，在产品仓库根目录可核对文件字节。

```bash
(cd ../gx-science/validation && sha256sum -c ../../product-multi-workflow/acceptance/multi_workflow/evidence/engineering/source-sha256.txt)
```

在产品仓库根目录复验合同、端点和入口/渲染用例可用以下命令。这会新建临时控制 Run。上文数字来自所列历史日志。

```bash
GX_CODEX_OFFLINE=1 PYTHONPATH=gx-h:. .venv/bin/python -B -m unittest -v \
  acceptance.multi_workflow.test_lead_scope_contract \
  acceptance.science_execution.study.test_signed_endpoint \
  acceptance.science_execution.study.test_signed_calibration \
  acceptance.science_execution.test_signed_native \
  acceptance.science_execution.test_research_entry \
  acceptance.science_execution.test_research_metric_text \
  acceptance.science_execution.test_research_routes
```
