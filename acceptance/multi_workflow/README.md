# 同题多路线研究的部署与复验

从仓库根目录运行。普通研究用 `bin/gx-p0 research` 提交问题、原件和资源配置。每个题目使用自己的状态目录和 run key。已部署的主机可直接从“启动与恢复”开始。

## 新主机准备

随附的离线依赖对应 Linux x86_64、CPython 3.11。宿主资产锁定官方 Codex CLI 0.169.0，Cordis 需要 Node ≥22.17。已验证的环境为 CPython 3.11.9、Node 22.17.0 和 npm 10.9.2。已有部署可复用同版本宿主。`GX_P0_CODEX` 可指向已有的官方可执行文件。源码 checkout 的部署示例如下，从本地已取得的官方平台包安装，脚本会核对锁中的摘要。[资产锁](../../release-assets/lock.json) · [离线包安装说明](../../RELEASE.md)

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --no-index --require-hashes \
  --find-links release-assets/python -r release-assets/python/requirements.lock
python -B scripts/release_assets.py all \
  --codex-archive /path/to/codex-0.169.0-linux-x64.tgz
```

坐标与方法插件已随仓库提供。以下命令核对三份示例 grant 引用的全部本地插件。原件路径、角色、SHA-256 与取得记录由相应 grant 给出，文件路径相对于该 grant。首航用 1VYM 和 9VGW，开放题另用 3JA9 和 3TBL。旧材料彩排用 2RE9 和 2O0O。只把入组资格与方法附在科学问题上，旧材料校准值和工程断言留在验证目录。[材料、来源与三套配置](../../examples/pcna/README.md)

```bash
for pkg in org.gx.investigation@1.0.0 org.gx.block-signed@1.0.0 \
           org.gx.block-influence@1.0.0 org.gx.protein@0.3.0 org.gx.research-survey@1.0.0
do
  bin/gx-p0 plugin verify "$pkg" || exit 1
done
```

真实研究复用官方 Codex 的 ChatGPT OAuth 登录。先确认本机 Clash 正在监听 HTTP/HTTPS `http://127.0.0.1:7890` 与 SOCKS5 `socks5h://127.0.0.1:7891`。所有外连均走 Clash，绕行限于 `localhost`、`127.0.0.1`、`::1`；监听不可用时停止联网。在私有终端执行 `bin/gx-p0 login-status`，需要登录时执行 `bin/gx-p0 login`。随后运行 `bin/gx-p0 doctor` 核对宿主、代理、账号与模型目录。OAuth 令牌、设备码、账号资料与原始宿主会话留在本机私有目录，公开证据使用科研报告和所需的事件标识。[宿主与登录说明](../../DEPLOYMENT.md)

## 启动与恢复

```bash
bin/gx-p0 research --state ./runs/pcna --run-key pcna-first-01 \
  --goal-file examples/pcna/first-flight.md --goal-config examples/pcna/goal.json \
  --research-effort medium
```

首航要求同一 Lead 委派两个调查模块。Lead 选择在哪份原件、哪种掩码和参数上登记可反驳的预测。当前版本的 `gx_stage_claim` 可从 `prediction.data_refs` 指定的原件取得共同的正式适用范围。若原件和登记的方法已完整界定预测，省略 `scope` 即可，中文科学解释可写入 `statement` 和 `limitations`。对于额外的人群或入组条件，应明确填写 `scope`，并等待符合条件的观测。

开放题让 Lead 自选路线和方法。彩排与开放题的完整命令见[三份问题卡](../../examples/pcna/README.md)。改用自己的原件时为新研究复制 grant，更新原件与取得记录，使用新的 run key。

中断后重发原命令，沿用原状态目录、run key、问题卡、grant 和宿主会话。`--timeout` 接受正秒数，只限制本次驱动；退出码 75 时保留当前进度。`bin/gx-p0 status --state ./runs/pcna` 可查看等待的工作。结题后重发原命令会从已采纳的关闭决定导出 `./runs/pcna/research/<run_id>/REPORT.md` 与 `result.json`。比较两次输出中的 `run_id`，结题重开的 `invocation.action` 应为 `export_closed`，`invocation.added.jobs` 与 `invocation.added.host_turns` 均应为 0。

## 本地聚焦复验

以下命令在已激活的虚拟环境中运行。输出写入新的临时验证目录，离线模式只在子 shell 中生效。CPU 并发验证需要进程实际获配至少两个可用逻辑 CPU。

```bash
gx_check_dir=$(mktemp -d)
(
  set -e
  export GX_CODEX_OFFLINE=1 PYTHONPATH=.:gx-h:acceptance/v62a
  python -B -m unittest -v acceptance.multi_workflow.test_lead_scope_contract
  python -B -m unittest -v \
    acceptance.science_execution.study.test_signed_endpoint.SignedEndpointTests
  GX_SIGNED_NATIVE_RECEIPTS="$gx_check_dir/signed" python -B -m unittest -v \
    acceptance.science_execution.test_signed_native
  python -B -m unittest -v tests.test_v6_prospective_jobs acceptance.multi_workflow.test_prediction_scope
  python -B acceptance/multi_workflow/parallel_jobs.py --work "$gx_check_dir/cpu"
)
```

| 复验 | 通过时可以核对的证据 |
|---|---|
| 原始 Lead 合同 7 项 | 子模块私有写入后，原父任务可冷恢复、再开分支、采纳原预测或引用已发布反证。过时修订须保留已持久化的子节点，新增开放模块使已准备的关闭操作失效。重复回调保留同一回执。[合同原文](test_lead_scope_contract.py) |
| signed 算法 5 组与正常路径 5 项 | 算法覆盖阈值、方向、中位次序、分母与缺测。正常路径检查阳性、阴性、缺测、错材料及已计算的旧表。查看 `$gx_check_dir/signed/` 中的输出、登记事件、送达读数与正式关闭回执。可推得数值的旧表会留下首次可用时间，缺测表保留未知。[算法对照](../science_execution/study/test_signed_endpoint.py) · [端到端对照](../science_execution/test_signed_native.py) |
| 预测与 Job 时序 | 覆盖开场等待、对应材料和中间表、无关 sibling 继续运行，以及生产启动早于登记时的判定。[时序案例](../../gx-h/tests/test_v6_prospective_jobs.py) |
| 双 CPU 与冷恢复 | 在 `$gx_check_dir/cpu/observed.json` 核对两个不同的 worker PID 和 CPU 绑定、正的 `compute_overlap_ns` 和 `concurrent_cpu_lower_bound_ns`。夹具在一个 Job 成功、另一个仍运行时中断协调器，随后核对原 Run、原 Job ID、已成功产物及 `extra_job_attempts == 0`。[执行与断言](parallel_jobs.py) |

## 真实研究证据

在该 Run 导出的 `result.json` 核对以下记录。对公开分享的报告保留证据引用与时间区间，宿主原始记录可在部署机上按 thread/turn 私下核对。[报告导出](../../gx-h/gx_core/p0/research_report.py) · [路线与交接证据](../../gx-h/gx_core/p0/research_routes.py)

- **同题双路线。** 在 `routes.modules` 核对同一 Goal 版本、同轮同父的两个模块，各自有不同的 scope、研究问题、实际运行成功的 Job、具名产物，以及研究者据此采纳的行动。`REUSED` 记录可证明复用，新增测量应指向其实际 Job。
- **回合重叠。** 在 `routes.real_turn_overlaps` 核对两个不同模块和线程的 `NATIVE_MODEL` 回合。两边均须有官方宿主的模型响应与起止时间，且两段时间交集大于零。缺一端时间时记录为未证实。`routes.job_turn_overlaps` 另列 Job 与另一模块回合的交集。CPU 屏障、本地脚本化 Responses 和排队时段分别只支撑各自的工程观察。
- **交接改变动作。** 在 `routes.handoffs` 跟随发布的报告引用、接收方实际收到的证据、此后的采纳决定与 `effects`。真实交接应有 `status=ENACTED`、`real_model_handoff=true`，并可指出新增量测、收窄判断或有依据的后续发现。
- **结论与恢复。** 将当前 Goal 版本生效的 `close_run`、决定引用与关闭事件对上，核对登记时点、材料、单位、分母、缺测和结题实际引用的端点。把原报告与同 key 重开后导出的调用增量一同保存。实测没有形成的证据保持未证实。

随包的真实旧材料彩排 `gxr_01m8cftps3hcwzxvz3` 提供了可直接阅读的研究报告、逐回合和逐 Job 证据，以及独立的同 key 重开收据。[彩排报告](evidence/rehearsal/REPORT.md) · [路线与采纳动作](evidence/rehearsal/result.json) · [恢复增量](evidence/rehearsal/warm-reopen.json) · [本地工程对照](evidence/engineering/README.md)

本次工作区另存验证索引于仓库相邻的 `../gx-science/validation/`。其中 `A/index.json` 收录原合同与预测时序，`B/barrier-summary.json` 收录双 CPU 及重开，`C/e2e/` 收录 signed 的合成原件对照。其他部署可用上述命令在自己的验证目录重新生成证据。

## 本次三项真实研究

三次从独立 run key 和新模型线程进入同一产品入口，均有正式关闭决定。科学产物附原始输出摘要、发布件和已关闭状态的重开记录，普通 OAuth 用量在后台记录。

| 研究 | 真实 Run | 官方模型回合 | 成功 CPU Job | 首次结题事件 | 同 Run 重开 |
|---|---|---:|---:|---:|---|
| [旧材料彩排](evidence/rehearsal/REPORT.md) | gxr_01m8cftps3hcwzxvz3 | 8 | 11 | 406 | +0 回合、+0 Job |
| [新材料双路线首航](evidence/first-flight/SCIENCE.md) | gxr_01m8cgr15h0txt5abp | 13 | 14 | 641 | +0 回合、+0 Job |
| [开放题自选路线](evidence/open-question/SCIENCE.md) | gxr_01m8ck7s94gn2qcw5q | 9 | 21 | 664 | +0 回合、+0 Job |

首航第二轮同一父任务的两个研究者各自产出测量，四段官方回合重叠中最长 225.292 秒。第三轮收到两份报告后做了四次共同主链复测，最终决定使用了 19 个已送达端点。首轮两个无模型回合的部分报告，以及两个因材料范围未匹配而未决的原预测，保存在同一个 Run。[首航原始报告](evidence/first-flight/REPORT.md) · [逐项登记与实测](evidence/first-flight/predictions.md) · [独立复算](evidence/first-flight/independent/READINGS.md)

开放题 Lead 自行决定由一个研究者检查全链覆盖，主线做删段测量，再按读数追加分散删点对照和逐链查表。共同 Cα 的 RMSD 出现了有分母的反例。3JA9 的 22 Å EM 模型使用 1VYM 刚体拟合，独立复核另列模板依赖、坐标精度及阈值附近的分类变化。[开放题原始报告](evidence/open-question/REPORT.md) · [独立复算](evidence/open-question/independent/READINGS.md) · [跨沉积模板比较](evidence/open-question/independent/TEMPLATE.md)

从仓库根目录可离线核对三个压缩包中的原始 Job 输出、报告、关闭决定、回合重叠和重开增量。独立数值实现使用锁定环境的 NumPy 与 Biopython，从原件重新拟合。[独立数值复验命令](independent/README.md)

```bash
for study in rehearsal first-flight open-question; do
  python3.11 -B acceptance/multi_workflow/verify_published.py \
    "acceptance/multi_workflow/evidence/$study" || exit 1
done
```
