# PI 使用手册：在网页和 CLI 中继续同一项研究

各组实际 Run、原始失败、修后签收和科学结果见[联合验收报告](ACCEPTANCE.md)。

下面的示例从空状态目录开始。课题使用公开的 1VYM 和 9VGW 坐标，研究人 PCNA 三聚体界面的报告方式。PI 可以在两条路线运行时给其中一条补充线索，并查看研究者后来怎样处理这条意见。父题封存后，3TBL 可作为后继题的新材料。[首航问题](../../examples/pcna-conversation/question.md) · [配置与原件](../../examples/pcna-conversation/goal.json) · [后继问题](../../examples/pcna-conversation/successor.md)

## 1. 从空目录启动并打开网页

在部署主机的 Bash 中运行。以下命令使用本地已有的 Python 3.11 环境、锁定版本的官方 Codex、Node 和已安装插件。新主机先按部署说明安装这些资产。[本地部署](../multi_workflow/README.md#新主机准备)

```bash
cd /inspire/ssd/project/sais-lifescience/public/ash_proj/gx-auto/gx-v-6/product-multi-workflow
gx_root=$PWD
source .venv/bin/activate
umask 077
gx_work=$(mktemp -d)
gx_state="$gx_work/state"
gx_key=pcna-pi-01
printf '研究目录 %s\n' "$gx_work"
```

示例沿用部署账户的官方 ChatGPT OAuth 订阅。已登录凭据由官方 Codex 从现有 `CODEX_HOME` 或默认的 `~/.codex` 读取。下面的函数用联跑所用的宿主环境启动普通 GX 命令。它固定 `openai`、ChatGPT 登录方式、仓库锁定的可执行文件和本次研究的会话目录。子进程只获得显式构造的环境，API key 和备用模型端点不会被传入。HTTP/HTTPS 经过 `http://127.0.0.1:7890`，SOCKS5 经过 `socks5h://127.0.0.1:7891`，绕行仅限本机回环地址。若 Clash 不可用，应先恢复上述监听再继续联网。[宿主环境](live.py) · [OAuth 与代理](../v623/oauth/host.py)

```bash
gx_oauth() {
  "$gx_root/.venv/bin/python" -B -c '
from pathlib import Path
import os, sys
from acceptance.unified_multiturn.live import environment
env = environment(Path(sys.argv[1]).resolve())
program = str(Path(sys.argv[2]).resolve())
os.execve(program, [program, *sys.argv[3:]], env)
' "$gx_work" "$@"
}
gx_oauth "$gx_root/bin/gx-p0" login-status
```

在确认输出的 `chatgpt_oauth` 为 `true` 后启动。当前登录和工作目录的路径应在恢复时继续使用。请将下列回执保存于自己的研究目录，额外付费需要 PI 另行授权。

```bash
gx_oauth "$gx_root/bin/gx-p0" research --state "$gx_state" --run-key "$gx_key" \
  --goal-file examples/pcna-conversation/question.md \
  --goal-config examples/pcna-conversation/goal.json --research-effort medium \
  >"$gx_work/driver.json" 2>"$gx_work/driver.log" &
gx_driver_pid=$!
```

`status` 是本地只读命令。此时目录中只有一项研究，准入完成后响应的顶层 `run_id` 就是本次真实 Run。下面在准入期间等候，一旦启动进程退出便可从日志检查原因。

```bash
until gx_run=$(bin/gx-p0 status --state "$gx_state" | python -c \
  'import json,sys; print(json.load(sys.stdin)["run_id"])' 2>/dev/null); do
  if ! kill -0 "$gx_driver_pid" 2>/dev/null; then
    cat "$gx_work/driver.log" >&2
    break
  fi
  sleep 1
done
printf 'Run %s\n' "$gx_run"
```

取得 Run ID 后，运行网页桥接并打开终端打印的本机地址。示例 URL 为 `http://127.0.0.1:8080/`。指定 `--port 0` 会由系统选择空闲端口。网页绑定同一绝对状态目录和本次 Run，运行期间可以关闭、重新打开网页。研究进程继续持有科研任务。

```bash
gx_oauth "$gx_root/gx-h/bin/gx-web" --state-dir "$gx_state" \
  --project pcna-multiturn --run-id "$gx_run" --port 8080 &
```

## 2. 交替用网页和 CLI

在网页发“目前哪条路线有实测，依据是什么？”。在 CLI 指定报告语言，然后刷新网页查看同一条消息。每条独立消息使用自己的稳定 ID，重发时沿用原 ID、原文、运行和目标。消息列表展示发送时归属、Main 回复、引用以及后续科研处置。反馈的实际效果可以从该条消息的证据链接追到 Lead 决定或已发布报告。

```bash
bin/gx-p0 status --state "$gx_state" --run-id "$gx_run"
gx_oauth "$gx_root/bin/gx-p0" message --state "$gx_state" --run-id "$gx_run" \
  --message-id pi-language-01 --text '以后请用中文呈现这项研究的报告。'
bin/gx-p0 conversation --state "$gx_state" --run-id "$gx_run"
```

在网页的目标选择中选出正在工作的真实路线并发送质疑。CLI 的等效参数为 `--branch-id`。追问已有消息使用 `--reply-to <原消息 ID>`。引用研究者提问使用 `--question-id <问题 ID>`；追问或延后选择会保留等待。明确采纳某个列出的选项时，加上 `--answer-option <完整选项>`。网页也可选取选项并发送。自由文字回答沿用 Main 解读。补充 UTF-8 文本文件用 `--attachment <文件>`，引用已获准读取的原件用 `--material-ref <artifact_ref>`。对“我的哪句话改变了哪条判断”的追问应核对原消息、Lead 处置与实际行动。`--queue-only` 可以只提交并让现有 Driver 或网页桥接继续投递。

## 3. 恢复原研究及只读重导

另开终端时，先回到同一产品目录并激活原环境。将 `gx_work` 设为首次打印的绝对研究目录，设置 `gx_root=$PWD` 与 `gx_state="$gx_work/state"`，再定义第一节的 `gx_oauth` 函数。从 `bin/gx-p0 status --state "$gx_state"` 取回原 Run ID；多 Run 工作区在 `runs` 数组列出各题。恢复使用同一部署账户。原研究进程退出后执行以下命令。正在运行时可用 `status` 查进展。被显式暂停或因短时驱动窗口返回退出码 75 的研究可继续使用这个入口。

```bash
gx_oauth "$gx_root/bin/gx-p0" research --state "$gx_state" --run-id "$gx_run"
```

科学状态 `CLOSED` 后，可以询问“为什么保留另一条路线，请给当时的证据”。这会留下对话回合。单独重新导出封存报告时，直接使用同一条 `research` 命令或首航时的同 key、同配置命令。回执的 `invocation.action` 为 `export_closed`，`invocation.added.jobs` 和 `invocation.added.host_turns` 均应为 0。报告和结构化结果分别在 `$gx_state/research/$gx_run/REPORT.md` 和 `result.json`。早期状态若在封存后出现版本偏移，报告导出的 `closure.goal_version`、`closure.decision_ref` 和 `closure.closed_event` 指向原结题版本。[封存报告读取逻辑](../../gx-h/gx_core/p0/research_report.py)

## 4. 从封存父题创建后继研究

在父题页选择要沿用的原件，可通过后继请求附加新材料。以下 CLI 示例提交一个独立问题，指定带有取得记录的 3TBL，并沿用父题的项目与已授权能力。路径使用绝对路径。如果还需把父题原件交给子题，可把源于父题材料列表的引用写入配置的 `parent_material_refs`。[后继问题](../../examples/pcna-conversation/successor.md)

```bash
python - "$gx_root" "$gx_work/successor.json" <<'PY'
from pathlib import Path
import json, sys
source = Path(sys.argv[1]) / "examples/pcna/open-data/3TBL.cif"
spec = {"research_effort": "medium", "input_files": [{
    "path": str(source), "role": "structure", "acquisition": str(source.with_suffix(".json")),
    "sha256": "05a1488d313d9926f556f495ad155e3ebf7859b2fa3014bc68ae55465acdb776"
}]}
Path(sys.argv[2]).write_text(json.dumps(spec))
PY
gx_oauth "$gx_root/bin/gx-p0" message --state "$gx_state" --run-id "$gx_run" \
  --message-id pi-3tbl-successor-01 --text "$(cat examples/pcna-conversation/successor.md)" \
  --successor --successor-config "$gx_work/successor.json"
bin/gx-p0 conversation --state "$gx_state" --run-id "$gx_run"
```

同键重试会返回同一个后继 Run。父题消息的 `result.run_id` 对应子题，`parent_decision_ref` 对应关闭父题的决定。网页可在两题之间切换。子题取得 Run ID 后，用 `gx-p0 research --state "$gx_state" --run-id <子题 Run ID>` 从同一工作区驱动和导出子题。父题读数进入子题时属于既知背景。

已有 P0 科研 Run 可以用其原状态目录、自己的 `--project` 和 `--run-id` 附着同一网页。旧 Run 保留准入时的科学与收敛策略。已有原生项目桥接使用 `gx-h/bin/gx-web --state-dir <原项目目录> --project <原项目名>`。带 `project-driver.json` 的原生项目会沿用既有项目入口。[两类项目的选择逻辑](../../gx-h/gx_core/web/research.py)

## 5. 离线验收与官方宿主实跑

在产品根目录执行。下列单元测试只使用本地状态；方法与停滞对照会实际运行本地 CPU 测量。仓库已发表的科研档案可以单独复核。

```bash
GX_CODEX_OFFLINE=1 PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a \
  .venv/bin/python -B -m unittest discover -v -s acceptance/multiturn
GX_CODEX_OFFLINE=1 PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a \
  .venv/bin/python -B -m unittest discover -v -s acceptance/unified_multiturn
GX_CODEX_OFFLINE=1 PYTHONPATH=.:gx-h:gx-h/tests:acceptance/v62a \
  .venv/bin/python -B -m unittest -v acceptance.v62b.test_rounds \
  acceptance.multi_workflow.test_advisory_rounds acceptance.multi_workflow.test_multiturn_method_survey
.venv/bin/python -B acceptance/multi_workflow/verify_published.py acceptance/multi_workflow/evidence/first-flight
.venv/bin/python -B acceptance/multi_workflow/verify_published.py acceptance/multi_workflow/evidence/open-question
```

自动操作网页时需要本机安装的 Playwright 与 Chromium。下列变量是本部署机上可用的路径，在其他机器上替换为相应安装目录、可执行文件。Chromium 的测试页面只访问本机 GX 网页。浏览器离线彩排会新建独立的状态目录。

```bash
export GX_PLAYWRIGHT=/tmp/gx-workbench-qa-44pAV5/playwright/package
export GX_CHROMIUM=/tmp/gx-workbench-qa-44pAV5/chromium
gx_probe=$(mktemp -d)
node acceptance/unified_multiturn/browser_smoke.cjs "$gx_probe"
```

**实际 A6 使用独立目录和新的 run key。** 下列命令会使用现有 OAuth 订阅、官方宿主、真实网页和 CPU 工作进程。`study` 用 0.001 秒驱动窗口完成准入交接，随后 `continue-study` 和浏览器 `participate` 在同一父题上并行运行。父题封存后，脚本执行后继研究、父报告重导与证据核对。人工首航和自动首航是两个独立的运行方式。

```bash
gx_live=$(mktemp -d)
gx_live_key=unified-pcna-01
.venv/bin/python -B -m acceptance.unified_multiturn.live all \
  --work "$gx_live" --run-key "$gx_live_key"
```

若分步操作，`study` 与 `participate` 在两个终端并行，两者使用同一组 `--work` 和 `--run-key`。首段短时驱动退出后，`continue-study` 继续同一个父题；父题封存之后依次运行 `followup`、`reopen`、`verify`。保留首次尝试和已有状态。独立运行 `verify` 读取既有证据。科学总结在 `$gx_live/RESEARCH.md`，证据在 `live-evidence.json`，PI 输入与后继过程分别在 `pi.json`、`followup.json`。A6 的签收依据包括真实模型回合的时间交集、PI 反馈到达时的两条在途路线、Lead 的实际处置、另一条原路线的结果、后继实测和零增量父报告重导。请以对应运行的核对结果为准。[联跑入口](live.py) · [证据检查项](live_receipts.py)
