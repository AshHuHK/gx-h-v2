# GAP-03 角色、模型与工具验收

官方 Codex 0.169.0 在两个独立本机 Responses 端点完成同一项目。主线程和 planner 请求 `ultra`，脚本模型目录将其映射到传输层的 `xhigh`。证据审查员在第二端点请求 `max`，线上报文中只声明 `gx_read_artifact` 和 `gx_submit_product` 两个 GX 工具。两端分别收到 8 个请求，GX 的 3 个原生 turn 分别结算 35、280 和 245 个夹具 token。[项目记录](summary.json) · [主端点报文摘要](primary.jsonl) · [审查端点报文摘要](review.jsonl)

公开入口执行了 `start → stop → start → resume → run`。第二次 start 时原配置文件已经删除。项目继续沿用已保存的角色分工，已完成的项目重开后新增请求为零。另一份独立状态复验得到相同的两端请求数和费用，并通过 `profiles --state` 读回每条线程的请求与有效设置。[重复运行](repeat/summary.json)

本机生产配置仅选中 OpenAI，尚无第二厂商条目。无配置的 provider 在本地读取阶段返回状态 1。官方目录未声明的模型在发起模型请求前被拒绝，报文计数为零。第二生产厂商的真实模型、压缩后简报、跨进程恢复与厂商计量均未测。锁定版客户端的非 OpenAI 压缩与恢复有独立的回环协议记录。[本机生产配置](local-configuration.json) · [不兼容型号观测](catalog-rejection.json) · [压缩协议](../native/compact_local.json)

在包根目录运行以下命令可生成新的证据目录。

```bash
PYTHONDONTWRITEBYTECODE=1 python acceptance/profile_routing.py --output /tmp/gx-profile-repeat
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=gx-h python -m unittest discover -s gx-h/tests -p test_p0_model_profiles.py
```
