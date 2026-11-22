# v2.1 复用和替换

- 保留官方 Codex 0.169.0 的 thread、turn、child、stdio 及会话恢复。所有外网经部署机 Clash。
- 复用原 SQLite 单写事务、Root delivery receipt、Outbox、JobService、ArtifactService、Execution Manifest、budget reserve/settle/open_child、lease renew、WorkflowRegistry。
- 替换结构化操作强制使用模型、遗漏回执永久占款、按节点同步复算、P0 对 TeamService 私有方法的直接调用、单状态目录只能查询一个 Run 的假设。
- 已有科研产物、旧 workflow pin、用户输入、原费用和已取消的执行权保持历史语义。允许用等价接口恢复，不给历史数据补造 provider 回执。
- 可选 Cordis/DSH 以及旧 native/CLI 入口保留兼容。按静态与启动时实际 import 分类，删除须有真实调用方证据。通用计算继续由已有 Job owner 管理。
