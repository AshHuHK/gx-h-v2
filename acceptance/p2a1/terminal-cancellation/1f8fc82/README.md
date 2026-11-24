# 终止提交的官方取消回执

`1f8fc82` 只改动终止协议验收夹具。它与 `3940eb5` 的 `gx-h` 产品树相同，均为 `9abe03abef1955e7234e61b7159bf683064ddb9f`。本次使用锁定的 Codex 0.169.0、真实 GX 提交与预算账本、隔离网络中的 Clash 和脚本化本地 Responses 服务。[源码与运行时身份](provenance.json)

## 原失败

`3940eb5/GX_ABORT_0060` 已记录 REJECTED 提交、同线程和 Turn 的 interrupted 历史，以及同一 `gx_submit_product` 调用的失败空结果。原监视器没有收到动态工具完成帧或原始函数输出。夹具在尝试读取官方持久回执前就因缺少实时帧退出。临时目录随后清理，无法确认这个历史 trial 的确切取消原因。保留其失败状态。[历史评估](original-3940eb5-assessment.json) [原始部分回执](original-3940eb5.partial.json.gz)

## 证明条件

公开历史须指向原线程、原 Turn 和原 call ID，工具须为 `gx_submit_product`，Turn 已中断，工具结果为失败且内容为空。夹具优先核对同作用域的原始函数输出。如果它缺失，必须在这次运行的官方 core rollout 中找到唯一的 `item_completed / DynamicToolCall`，其线程、Turn、call、工具、失败状态、空内容均匹配，错误为原文 `dynamic tool call was cancelled before receiving a response`。存在的实时镜像也必须一致。正常 ToolResponse 或提交后模型请求都会使判据失败。

官方 v2 将 core 动态调用投影为公共历史项时会省略 error 字段。夹具现在保存每次持久回执检查的文件名和哈希、同 Turn 或同调用记录摘要、精确匹配的 core 原件。今后若缺少原件，失败试验的部分回执仍会记录已检查的范围。

## 冻结结果

| 验证 | 实测 |
|---|---|
| 原 204 组 | 204 通过，103 ACCEPTED、101 REJECTED |
| 取消原因来源 | 202 组读到原始函数输出；1 组从 core 证实且收到实时动态项；1 组从 core 证实且两个实时镜像均缺失 |
| 业务副作用 | 206 次原有模型请求，提交后 0 次，正常 ToolResponse 0 次。204 个原预算预约对应 204 条唯一结算。其中 2 条保有 ESTIMATED 置信度 |
| 受控丢帧 | 对同一真实调用同时丢弃动态完成项与函数输出，官方 core 仍记录精确取消原因。原提交只结算一次，35 tokens |
| 反证 | 11 组不充分或矛盾的回放均拒绝，包括缺原件、跨线程或 Turn、错误 call / 工具 / 取消原因、重复 core、普通回复、提交后请求和矛盾的公开历史 |

在自然双镜像缺失的 `GX_ABORT_0136` 中，原线程是 `01a20eb8-85ac-74f2-8ad7-3d7a1199fc7c`，原 Turn 是 `01a20eb8-85bd-7ac3-a0eb-5103fb51f649`，调用是 `call-probe-response-136`。该次官方记录保留了原始取消原因，只有一条结算事件 1515。[两条自然 core 证明](natural-core-proofs.json) [204 组结果汇总](full-summary.json) [全部原始试验](full.json.gz)

受控丢帧使用真实的线缆帧。反证在该冻结回执的副本上修改单个输入或移除记录。[受控丢帧原文](lost-mirrors.json) [反证结果](negative-controls.json) [反证重放脚本](negative_controls.py)

本目录的 `manifest.sha256` 可用 `sha256sum -c manifest.sha256` 校验。204 组与双丢帧的完整命令、命名空间、代理版本及退出码分别保存在 [完整运行回执](full-network.json) 和 [丢帧运行回执](lost-mirrors-network.json)。
