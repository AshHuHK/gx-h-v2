# GX 复用与改造

| 部件 | 实施处置 | 原因 |
|---|---|---|
| SQLite、预算、租约、作业收据、不可变 Artifact | 复用原 owner | 事务、幂等键、真实外部作业状态均由已有服务管理。 |
| Workflow、checkpoint、提交验证 | 在 science_grid 正常路径增加 P0 结算 | 接受独立任务产物后才能改变路线。 |
| GX App Server stdio | 复用同步传输，增加官方 Root 绑定及交互方法 | 已处理双向 JSONL、独立请求 ID、超时和进程清理。 |
| 人类授权、Intent 版本、影响、Manifest | 补充持久表及写入，关联原项目 ID | 旧库缺少这些可提交的业务记录。 |
| 包内 codex_adapter 与 gx_reference | 保留设计对照 | 生产入口使用 GX store 与已验证的协议客户端。 |
| 原 DSH、Cordis、源码宿主和旧的原生子代理派单入口 | 保留历史发布内容，新 P0 执行图只连接官方 App Server | 旧版测试和使用者可继续从原入口运行。 |

基线和项目数据位于不同目录。本次无删除历史模块或改写 Codex 核心的步骤。
