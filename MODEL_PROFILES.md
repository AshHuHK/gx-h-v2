# Codex 模型与工具配置

默认项目通过官方 Codex 的 ChatGPT OAuth 使用 `openai/gpt-6.2-astra`。启动新项目前可用 `GX_P0_PROVIDER` 和 `GX_P0_MODEL` 指定全项目默认值，或用 `GX_P0_PROFILE` 指向一个 UTF-8 JSON 文件。GX 通过同一个 0.169.0 App Server 为各个任务创建线程。已有项目会从数据库恢复其原有分工。

下面的配置让证据审查员使用另一款 OpenAI 模型，并把该角色的 GX 工具限制为读取原始产物和提交独立判断。队员仍然能通过原任务的 brief 取得上下文。需要运行中读取更新任务书的角色应包含 `gx_task_context`。省略 `tools` 时沿用角色在当前工作流中配备的全部 GX 工具。

```json
{
  "default": {"provider": "openai", "model": "gpt-6.2-astra"},
  "roles": {
    "evidence_reviewer": {
      "model": "gpt-6.2-sol",
      "tools": ["gx_read_artifact", "gx_task_context", "gx_submit_product"]
    }
  },
  "task_classes": {
    "AUTHORING": {"effort": "max"}
  }
}
```

`science_grid@3` 的 `evidence_reviewer` 与 `planner` 默认可以读取任务和产物、提交结果。已绑定的网格插件还会提供 `gx_grid_measurement`。`researcher` 与 `executor` 可以查看能力、提交和查询分配的 Job，也可以等待结果。研究员还可以取消自己的 Job。各角色默认单独执行。某项分工需要原生子代理时，可在对应角色设置中加入 `"multi_agent": true`。直接子代理继承父线程的工具目录，GX 允许其读取分配给父线程的原始产物，提交成果和管理 Job 仍由父线程负责。

字段按 `default`、`task_classes`、`roles` 的顺序覆盖。`profiles` 会列出可用角色。任务类别包括 `PLANNING`、`AUTHORING`、`VERIFICATION`、`EXECUTION` 和 `TESTING`。省略 `effort` 时，协调和决策角色默认 `ultra`，复杂科研、验证和代码工作默认 `max`，简单执行和测试默认 `xhigh`。本地测试模式的默认值也是 `xhigh`。显式填写时可使用所选模型目录中的档位名。新生产项目由官方 `model/list` 核对目标模型与档位；冷恢复沿用该项目冻结的角色设置。工具名必须来自该 Root 的实际 GX 工具，持久 Worker 需要 `gx_submit_product` 交付成果。

同一工作区的 Run 共享供应商正式发布的限流窗口。GX 保留原任务和等待中的租约，窗口重置后继续准入。部署可以在 `gx-h/config/limits.json` 的 `native_provider.parallel_turns` 中为指定供应商设置并发上限。新项目接入缺少限额反馈的供应商时默认并发为 1，可通过项目的 `max_parallel_roots` 显式设置。窗口缺少重置时间时，GX 按 `native_provider.retry_seconds` 安排下一次尝试，默认 60 秒。

```bash
export GX_P0_PROFILE=/path/to/roles.json
bin/gx-p0 profiles
bin/gx-p0 doctor --role evidence_reviewer
bin/gx-p0 start --state /path/to/new-state --spec /path/to/project.json
bin/gx-p0 profiles --state /path/to/new-state
```

`profiles` 只读本地配置，不发出推理请求。`doctor` 通过 Clash 和官方客户端检查账号与模型目录。`doctor --role evidence_reviewer --smoke` 会实际发起一次简短推理并报告线程回执。省略 `--role` 的 smoke 使用默认线程的模型与 `xhigh`。项目的自定义配置也在正式启动时核对各 provider 的 `model/list`。未声明模型或推理档位会返回对应的 provider、模型和档位。Codex 可以使用本地 `model_catalog_json` 或缓存，因此目录中的能力仍需用真实服务验证。

## 自定义 Responses 服务

把生产服务注册到操作员的 `$CODEX_HOME/config.toml` 或 `~/.codex/config.toml`，然后把角色的 `provider` 改为该节的实际 ID，并填写厂商确认支持的 `model`。0.169.0 的相应配置形式如下。其中域名、服务名、模型及环境变量名要按实际部署填写。

```toml
[model_providers.research_service]
name = "Research service"
base_url = "https://responses.example.invalid/v1"
wire_api = "responses"
env_key = "RESEARCH_SERVICE_TOKEN"
```

厂商凭据由官方客户端从其环境或认证配置中取得。默认 OpenAI 模型仍使用 `bin/gx-p0 login` 的官方 ChatGPT 登录。自定义服务须支持原生 Responses 流、所选模型与档位、科学角色需要的工具调用。示例生产地址使用 HTTPS，所有远端请求均经指定 Clash。回环协议夹具使用本地 HTTP。`GX_P0_TEST_PROVIDER` 只用于 `GX_P0_TEST=1` 的回环服务。

本机官方客户端的非 OpenAI 压缩协议验收通过受控回环端点，在同一线程压缩和跨进程恢复后分别读回了任务简报、工具目录和本地摘要。生产第二厂商尚未配置，其服务响应、压缩后简报、恢复和计量均待实际接入时验证。[协议观测](acceptance/runs/native/compact_local.json) · [型号未被目录声明时的结果](acceptance/runs/acc-1-profiles/catalog-rejection.json)

`profiles --state` 只读项目状态，列出冻结的角色分工与各线程请求和生效的配置。每个新 Root 的 `p0_roots` 行保留请求的 `provider/model/effort` 和 `tool_names_json`。`effective_profile_json` 来自最近一次官方 `thread/start` 或 `thread/resume` 响应。具体推理请求的 wire effort 取决于模型目录，尤其是 `ultra` 会由官方客户端映射到该模型的多代理推理档位。通过 `thread_id`、`turn_id` 和 `reservation_ref` 可核对实际用量。既有 Root 在迁移时保留原始值，新观测到的回执才填入有效配置。[档位语义](audit/OAUTH_EFFORT.md) · [本机双端点复验](acceptance/runs/acc-1-profiles/summary.json) · [当前机器上的生产 Provider 配置](acceptance/runs/acc-1-profiles/local-configuration.json)
