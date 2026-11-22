# GX + Codex Autonomous Science Harness — P0 Implementation Package

本包是交给 Coding Agent 的**实施规格 + Codex App Server 接入参考代码 + 验收合同**。
它不是声称已经完成的 GX 实现，也不是 Codex 上游源码的复制品。

## 目标架构

- **Codex**：未修改的官方 Agent Runtime。保留原生 Agent Loop、原生 Subagent、Thread、Session、Tool Orchestrator、Compaction、Sandbox。
- **GX**：精简的 Scientific Execution Kernel。只负责正式 Intent、持久任务执行、Job、资源准入、执行清单、证据和事件推进。
- **Codex 原生 Subagent**：用于单个执行上下文内的临时协作；GX 不追踪、不接管其任务生命周期。
- **GX Durable Task**：当工作需要跨 Thread、等待外部 Job、被人类需求变更影响、需要租约/预算/恢复时，GX 创建独立 Codex Root Thread。
- **Skills**：科研方法、角色说明、操作规程。不得用 Skill 替代确定性状态、预算强制、Job 对账或证据来源。

## 包含内容

| 路径 | 内容 |
|---|---|
| `DESIGN_SPEC.md` | 完整 P0 架构与多轮消息机制 |
| `IMPLEMENTATION_PROMPT.md` | 可直接交给 Coding Agent 的总 Prompt |
| `CODEX_SOURCE_AND_API.md` | Codex 源码版本、源码地图、接口核验要求 |
| `codex_adapter/app_server.py` | Codex App Server JSON-RPC Python 参考客户端 |
| `gx_reference/schema.sql` | Intent、授权、变更、Outbox、Task/Attempt、Job、Evidence、Execution Manifest 的最小参考 Schema |
| `gx_reference/intent_policy.py` | 变更分类与提交政策参考实现 |
| `scripts/fetch_codex_source.sh` | 获取并核验指定 Codex 源码基线 |
| `acceptance/ACCEPTANCE_MATRIX.md` | P0 功能、故障注入与验收矩阵 |

## 重要边界

1. 不 Fork、不修改 Codex 核心；优先通过官方 App Server 接入。
2. 本包的 `codex_adapter` 是 GX 到 Codex 的**客户端参考代码**，不是 Codex 源码。
3. `rust-v0.169.0` 与 commit 前缀 `d32063f9` 来自本项目已提供蓝图的静态审计基线。实施 Agent 必须实际检出并核对；不匹配时停止版本相关实现并报告，不可静默换版本。
4. 当前公开 App Server 文档可作为协议导航，但最终以锁定版本生成的 schema 和源码为准：
   - https://learn.chatgpt.com/docs/app-server
   - https://github.com/openai/codex
5. 不把未经运行验证的接口、状态枚举、恢复语义写成已实现事实。

## 快速使用

```bash
# 1. 解压到 GX 仓库工作区外层或独立实施目录
# 2. 获取 Codex 源码基线（如环境可访问 GitHub）
bash scripts/fetch_codex_source.sh ./vendor/codex

# 3. 把 IMPLEMENTATION_PROMPT.md 完整交给 Coding Agent
# 4. 要求 Agent 先审计当前 GX 仓库和 Codex 源码，再提交实施计划
```

## 不在本包范围内

- 不重写 Codex Agent Loop / Subagent / Thread Manager。
- 不重建 DSH / Cordis。
- 不预先建设完整通用科研知识图谱、Agent Pool、Research Portfolio、Council、自动配方市场。
- 不承诺所有外部模型 API、GPU 任务都能 bitwise reproducible。
- 不允许 Agent 自行放宽人类硬约束或资源上限。
