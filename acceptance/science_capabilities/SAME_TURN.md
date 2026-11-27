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
