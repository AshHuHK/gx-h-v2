# Lead 在同一真实回合采用新提取的来源

Run `gxr_01m8db31ha1dfreqgq` 在候选提交 `0664513` 上从普通 `research` 入口启动并封存。官方 Codex 经 Clash 使用现有 ChatGPT OAuth。Lead 使用 `gpt-6.2-astra`、`max`，一个真实回合完成来源读取、提取和结题。题目要求验证同回合引用，科学判断由 Lead 形成。[原始题面](live/same-turn/question.md) · [能力配置](live/same-turn/grant.json) · [脱敏回执](evidence/native-source-citation.json)

正式报告指出，2RE9 以 X 射线衍射测定，此次精修采用的分辨率范围从低分辨率端 50.000 Å 到高分辨率端 2.100 Å。节录没有给出足以评价原始反射完整性、单个原子坐标精度或生物学功能的证据。相应局限以 `insufficient` 写入正式主张。这次研究直接读取了公开沉积元数据，提交的计算 Job 为 0。[公开节录](../../examples/plugins/v62_public_materials/deposition-excerpt.txt)

| 实际发生的步骤 | 核对依据 |
|---|---|
| 阅读原件 | GX 返回段落 `p3`，对应原件第 6–8 行。交付给模型的段落及其 SHA-256 与 GX 回执一致。 |
| 提取 | GX 在事件 19 为逐字定位的精修字段生成 `gxa_01m8db4ma6a3498vhp`。官方宿主记录中有完整的成功工具返回。 |
| 采用并结题 | Lead 在同一任务、租约、线程和回合把该新引用写入顶层 `evidence_refs`。GX 在事件 31 采纳 `close_run`，事件 33 正式封存。原文交付、提取交付、最终模型提交分别位于同一官方记录的第 24、35、78 行。 |
| 首次尝试与修正 | 两次决定草稿因结题参数和未知图操作被拒，第三次被采纳。同回合另外有三次主张草稿被拒，涉及主张可引用范围、数值写法和支持等级。GX 保留了首次结果。 |
| 正常重导 | 同一 Run 再次执行 `research --run-id`，返回 `export_closed`。报告 SHA-256 与结题决定保持一致，新增宿主回合 0，新增 Job 0。 |

本项验证的是顶层决定引用新证据并生效。模型把同一提取记录用作正式主张的新依据时，主张接口仍执行自己的范围与证据等级规则。正式关闭由 GX 的采纳回执及状态转换证明；官方宿主按现有协议中断已完成的回合。

## 复验已完成的 Run

在保有原状态与官方会话的部署机，从产品根目录执行。该命令只读核对原始提交和已交付的产物，并通过普通入口重导封存报告。

```sh
gx_work=../gx-science/capabilities-check/live-native-source-citation
.venv/bin/python -B acceptance/science_capabilities/verify_native_source_citation.py \
  "$gx_work" gxr_01m8db31ha1dfreqgq --output /tmp/gx-native-source-citation.json
```

## 独立复现首次启动

下列命令会使用一次新的官方订阅研究回合。原验收 Run 只启动了一次。新题从具有同一实现与验收材料的部署目录运行，代理采用该机既有 Clash 配置。

```sh
gx_work="$(mktemp -d -p /tmp gx-native-source-citation.XXXXXXXX)"
git rev-parse HEAD > "$gx_work/launch-release.txt"
.venv/bin/python -B acceptance/science_capabilities/gx_oauth.py "$gx_work" research \
  --state "$gx_work/state" --run-key same-turn-source-citation \
  --goal-file acceptance/science_capabilities/live/same-turn/question.md \
  --goal-config acceptance/science_capabilities/live/same-turn/grant.json \
  > "$gx_work/launch.json"
gx_run="$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["run_id"])' "$gx_work/launch.json")"
.venv/bin/python -B acceptance/science_capabilities/verify_native_source_citation.py \
  "$gx_work" "$gx_run" --output "$gx_work/receipt.json"
```

## 真实方法卡能在本回合被 Lead 引作方法依据

父题及后继的 5 次既有 `SCOPE_DENIED` 均按原始参数、任务、租约和回合进行了只读回放。官方 Codex 的 `function_call.arguments` 与 GX 保存的原参数逐字段相同。5 个被拒字段的对象都已由该次任务的早先调用送达。[5 条原始请求的回放](evidence/native-method-citation-replay.json)

| Run 与原拒绝事件 | 当时被拒的首个引用 | 修补后的引用检查 |
| --- | --- | --- |
| 海藻糖 30、75 | 分别为本回合的来源提取与证据复核 | 2/2 通过 |
| 蔗糖 178 | 精确查询返回的方法卡 `gxa_01m8dar7xdgdmj9g2g`。同一提交还引用了来源提取。 | 通过 |
| 蔗糖 179、229 | 分别为来源提取与证据复核。事件 229 的同一提交还附上了本任务已暂存的主张。 | 2/2 通过 |

在 `0664513` 的引用检查下，事件 178 是剩余的拒绝。Lead 当时引用具名方法来说明按登录号连接和保留空值的理由。核验所得卡片含版本、方法 SHA、分母、字段关系和局限。当时的方法尚未启用；同回合取得方法文档足以引用其出处，执行仍需要单独的能力选定和实际 Job。

GX 按同一 Run、任务、租约、线程和回合的精确查询查找方法依据。不同研究者可拿到同一公开卡，谁实际收到它由各自的原生请求及回执确定。Lead 以回执生成的正式公开卡核对原件字节。具有 `public_methods` 授权的子模组以实际收到的 `spec` 核对原件字节，同时匹配本模组的 `v62.module.tool_completed` 回执中的原件与完整返回摘要。幂等重送沿用原模组回执。原始五条只读回放均来自 Lead，历史决定与科学结论保留。

受派研究者的离线验收安装了获准查询与决策的测试插件，从正常 Goal 入口建立两个模组实例。第一名研究者查阅后，GX 接纳了其引用该卡的决定。第二名借用同一引用时被拒，在亲自查询后成功提交。伪造角色以及修改任一原生返回内容也触发拒绝。该组测试使用脚本传输及真实 GX 任务与租约。现有只读 survey 仍可在自己的模组报告引用已读方法，已有工具授权决定它能提交的操作。科学主张、图输入、量测值仍分别由原入口管理。[受派研究者回归](test_method_citations.py)

```bash
gx_checks=../gx-science/capabilities-check
.venv/bin/python -B acceptance/science_capabilities/replay_source_citations.py \
  "$gx_checks/live-record-coverage" "$gx_checks/live-record-successor" \
  --run-id gxr_01m8d9xyaych3h1z2b --run-id gxr_01m8daq09da52k64ar \
  --output /tmp/gx-native-method-citation-replay.json
cmp /tmp/gx-native-method-citation-replay.json acceptance/science_capabilities/evidence/native-method-citation-replay.json
```
