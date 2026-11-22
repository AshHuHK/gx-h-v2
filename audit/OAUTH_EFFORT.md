# OpenAI OAuth 与推理档位审计

2026-11-22 UTC，Linux x86_64。当前 ChatGPT 登录可以在官方 Codex CLI 0.169.0 上以 `gpt-6.2-astra` 完成推理。GX 选择这个已验证模型作为初始默认值。用户指定了角色档位，具体模型由本次实施选择。[发布包核验](../acceptance/runs/schema/cli-provenance.json) · [上游 tag 与 commit](../acceptance/runs/schema/checkout.json)

## 本机运行记录

| 探针 | 观测 |
| --- | --- |
| `codex --version`、App Server `initialize` | 两者均报告 0.169.0。锁定上游 commit 为 `d32063f96c2dff1dc96af839b3952abb6acfeda4`。 |
| `codex login status`、`account/read {"refreshToken":false}` | 分别报告 ChatGPT 已登录、账户类型 `chatgpt`。沿用 Codex 自有凭据存储。 |
| `model/list {"limit":100,"includeHidden":false}` | 当前 OpenAI provider 返回下表十个可见模型。该接口可能使用官方目录缓存，各模型的独立推理可用性需要实际请求验证。 |
| `thread/start`，三个临时线程 | `gpt-6.2-astra` / `openai` 分别回显 `max`、`ultra`、`xhigh`。前两项只创建线程。 |
| 唯一一条真实模型请求 | `xhigh` 线程 `01a20121-a7cf-7431-bd82-450f1f61422e`，turn `01a20121-a7fa-78d3-a29e-5019155c68cf`。收到 `turn/completed`，状态 `completed`，最终消息精确匹配 `GX_P0_OAUTH_OK`。从 turn/start 应答到终态 3.17 秒，Codex 报告输入 8,545 tokens、缓存输入 0、输出 10、其中推理输出 0。 |
| 传输观测 | 对 App Server 进程及子进程的 TCP socket 采样，只观察到对端 `127.0.0.1:7890`。同时核对下面的固定版本路由实现。 |

`max` 的临时线程为 `01a20121-a6f2-7aa2-9bb2-32606882d6d7`，`ultra` 为 `01a20121-a79e-7f41-8f01-fcdd802d5253`。启动传入 `modelProvider="openai"`、`config.model_reasoning_effort`、`ephemeral=true`，实际请求另传 `turn/start.effort="xhigh"`。部署后可用 `bin/gx-p0 doctor` 重读账号目录，`--smoke` 会显式新增一次模型调用。[真实调用记录](../acceptance/runs/oauth/live-xhigh.json) · [产品入口目录检查](../acceptance/runs/oauth/doctor.json) · [线程参数及响应](../vendor/codex/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L64) · [turn 参数](../vendor/codex/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L181)

| 模型目录报告的可选档位 | 本机可见模型 |
| --- | --- |
| `low`、`medium`、`high`、`xhigh`、`max`、`ultra` | `gpt-6.2-astra`、`gpt-6.2-sol`、`gpt-6.1-terra`、`gpt-6.1-sol`、`gpt-6-astra`、`gpt-6-sol`、`gpt-5.6-sol`、`gpt-5.6-terra` |
| `low`、`medium`、`high`、`xhigh`、`max` | `gpt-6-luna`、`gpt-5.6-luna` |

Codex 的普通推理请求直接传递 `max` 和 `xhigh`。`ultra` 是客户端的选项，在原生 V2 多代理模式中启用主动协作提示。构建普通 Responses 请求时，它优先采用模型声明的 `multi_agent_reasoning_effort`，缺省再按源码的次序回退。本机账户目录中 `gpt-6.2-astra` 的该字段为 `xhigh`。观测到的本地目录样本由另一个 0.171.0 客户端缓存；上表则来自此次 0.169.0 的 App Server 应答。这些记录能证明配置及回包，服务商对每项计算的内部执行深度不可观测。[上游 effort 映射](../vendor/codex/codex-rs/protocol/src/openai_models/reasoning_effort.rs#L10) · [上游多代理模式选择](../vendor/codex/codex-rs/core/src/session/multi_agents.rs#L82) · [目录处理](../vendor/codex/codex-rs/app-server/src/request_processors/catalog_processor.rs#L178)

## OAuth 及 Clash

DSH 指南第 17–25、80–109 行记载 DSH 自身可重用已获准的授权，设备码由人在 OpenAI 页面完成，指南的短文本探针使用 `gpt-6.1-sol`。本包使用官方 `codex login --device-auth` 和 App Server 的 `account/read`。现有 ChatGPT 登录可直接复用。GX 只消费账户类型、模型列表与公开协议状态，凭据的存储和更新由官方 CLI 持有。[项目提供的 OAuth 指南](../../../output/DSH-OpenAI-OAuth-接入指南.md) · [OpenAI 设备登录说明](https://learn.chatgpt.com/docs/auth)

CLI 的 `features list` 显示，默认 `respect_system_proxy=false`、`system_proxy_fallback=true`。本次显式运行 `codex --enable respect_system_proxy --disable system_proxy_fallback …`，再次读取功能列表得到 `true/false`。在 Linux 上，显式模式为 HTTPS/WSS 选取 `HTTPS_PROXY`，HTTP 选取 `HTTP_PROXY`，将 `NO_PROXY` 应用于相应传输。大小写代理变量均指向 HTTP `http://127.0.0.1:7890`、SOCKS5H `socks5h://127.0.0.1:7891`。本机的绕过表为 `localhost,127.0.0.1,::1`，本次请求前两个监听套接字均可连接。[配置和启动期路由](../vendor/codex/codex-rs/core/src/config/mod.rs#L1863) · [Linux 解析](../vendor/codex/codex-rs/http-client/src/outbound_proxy.rs#L381) · [WebSocket 连接](../vendor/codex/codex-rs/websocket-client/src/dialer.rs#L51)

## 对部署配置的影响

继承的 GX 角色投影默认采用 `gpt-6.1-sol`。P0 对组长和主协调者使用 `ultra`，对论证、规划、复杂代码和科学审查使用 `max`，对简单执行与测试使用 `xhigh`。验收启动统一指定 `GX_P0_TEST=1`，维持 `xhigh`。这套分类是代表用户作出的实现选择；新任务仍由协调者决定实际分解与分工。账号模型目录随授权、套餐和服务端更新而变化。
