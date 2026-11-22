# Codex Source & App Server Integration Guide

## 1. Source baseline

本项目蓝图静态审计采用：
- Repository: https://github.com/openai/codex
- Tag: `rust-v0.169.0`
- Expected commit prefix: `d32063f9`

这是已有蓝图提供的基线，不代表本文件作者已重新验证远端 tag。实施 Agent 必须执行：

```bash
git ls-remote https://github.com/openai/codex.git refs/tags/rust-v0.169.0
git clone --filter=blob:none --no-checkout https://github.com/openai/codex.git vendor/codex
git -C vendor/codex fetch --depth 1 origin tag rust-v0.169.0
git -C vendor/codex checkout --detach rust-v0.169.0
git -C vendor/codex rev-parse HEAD
```

如果 commit 不以 `d32063f9` 开头：
1. 不要静默接受新版本。
2. 保存实际 commit 和差异。
3. 先判断蓝图基线是否错误或 tag 被移动。
4. 请求项目负责人确认版本。

## 2. Important source areas from the supplied blueprint

以下路径是蓝图中的审计索引，实施时必须在实际 checkout 中核对：

| Area | Source path |
|---|---|
| Turn loop | `codex-rs/core/src/session/turn.rs` |
| AgentControl API | `codex-rs/core/src/agent/api.rs` |
| AgentControl runtime | `codex-rs/core/src/agent/control/runtime.rs` |
| Agent spawn | `codex-rs/core/src/agent/control/spawn.rs` |
| Mailbox | `codex-rs/core/src/agent/control/mailbox.rs` |
| ThreadManager | `codex-rs/core/src/thread_manager.rs` |
| ThreadStore | `codex-rs/thread-store/src/store.rs` |
| AgentGraphStore | `codex-rs/agent-graph-store/src/store.rs` |
| Tool orchestrator | `codex-rs/core/src/tools/orchestrator.rs` |
| Exec policy | `codex-rs/core/src/exec_policy.rs` |
| Compaction | `codex-rs/core/src/compact.rs` |
| App Server protocol | `codex-rs/app-server-protocol/src/protocol/` |
| App Server processors | `codex-rs/app-server/src/` |
| Goal extension (if present in pinned baseline) | `codex-rs/extensions/goal/` or actual path found by search |

Do not assume paths remain identical across versions.

## 3. App Server protocol workflow

App Server is a long-lived JSON-RPC client/server interface. For stdio:
- Start Codex App Server.
- Send `initialize`.
- Wait for successful response.
- Send `initialized`.
- Only then send normal requests.
- Read response messages and server notifications continuously.
- Correlate request IDs with responses.
- Persist important notifications/observations in GX.

Typical protocol methods to verify against pinned source:
- `thread/start`
- `thread/resume`
- `thread/turns/list`
- `thread/items/list`
- `turn/start`
- `turn/steer`
- `turn/interrupt`
- `thread/goal/set`
- `thread/goal/get`
- `thread/goal/clear`
- Thread/Turn/Item notifications

Official protocol documentation (use as navigation, not as substitute for pinned source):
https://learn.chatgpt.com/docs/app-server

## 4. Protocol behavior to explicitly verify

### 4.1 `turn/steer`
- Does it require an active turn?
- Does it require `expectedTurnId`?
- What happens if a long-running tool call is active?
- Does steer get consumed at a step boundary?
- What is the observed latency distribution?

### 4.2 Native Goal
- Is `thread/goal/set` available in pinned baseline?
- Exact params and status enum.
- Does setting a new objective reset token accounting?
- Does goal status `paused` exist and what does it do?
- Does Goal update inject a message into an active turn?
- Can a model-originated tool update Goal objective, or only status?
- How to disable Goal for the main conversation thread?
- Does Goal budget include child subagents? Verify with a test.
- How does Goal interact with Job waiting and `turn/start`?

### 4.3 Thread lifecycle and recovery
- What survives App Server process restart?
- Does `thread/resume` restore the same thread and tool configuration?
- How are dynamic tools persisted?
- How are descendant native Subagent threads restored?
- What happens to in-flight tools at process death?
- Which notifications are replayed or not replayed?
- How to distinguish a completed turn from an unknown outcome?

### 4.4 Human-input authorization
The supplied blueprint references `client_user_message_id` and thread/turn IDs. Verify the actual event and protocol fields in the pinned source. GX must derive authorization from trusted protocol envelopes / client ingress records, not from model-supplied tool arguments.

### 4.5 Compaction
Verify whether `developer_instructions` or equivalent task instructions are reinserted after compaction for:
- OpenAI provider
- non-OpenAI provider
- resumed thread
- forked thread

Do not assume behavior based on one provider.

## 5. Why use App Server rather than modify Codex

The intended integration boundary is the public App Server protocol. A separate GX client process:
- starts/resumes threads,
- starts/steers/interrupts turns,
- sets/reads/clears native thread goals,
- consumes notifications,
- invokes GX tools through a documented/verified tool bridge.

Do not embed GX into Codex Rust internals unless an experiment proves the external boundary cannot satisfy a required property. Any such change requires a separate ADR with:
- missing capability,
- measured failure,
- smallest viable patch,
- maintenance cost,
- rollback plan.

## 6. Code included in this package

`codex_adapter/app_server.py` is a small Python JSON-RPC reference client for the Codex App Server. It is integration code written for this package, **not a copy of Codex upstream source**.

The upstream source is fetched separately using `scripts/fetch_codex_source.sh`. This avoids shipping a stale or misleading partial copy of the full Codex repository.
