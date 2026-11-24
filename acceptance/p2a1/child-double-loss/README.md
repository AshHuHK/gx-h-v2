# P2A.1 原生子线程双丢帧冷恢复

候选版本 `eb0cd528129002a1b5e7a19fa4c3c5d515479ea6` 通过。历史版本 `61ea96daa218719a7663cb8fc8b404256560b124` 用同一套验收源码重现了缺陷。两个工作树都使用 SHA256 以 `b452ca20` 开头的官方 Codex 0.169.0、脚本化本地 Responses 服务和独立网络命名空间内的 Clash。网络回执记录完整哈希、命令、路由和退出码。

## 实际过程与判定

1. GX 提交并观察一个真实本地 CPU Grid Job。原始父 Turn 只委派一个官方 Codex 子线程。测试转发器截获两条真实 `subAgentActivity.started` 通知并丢弃，同时暂存真实的子 `turn/started`。官方子模型请求在 Responses 端等待。
2. 验收用 SIGKILL 结束 GX 进程。新 GX PID 打开同一 SQLite 后，转发器丢弃暂存的子开始帧，Responses 服务放行原有请求。原 App Server 随后报告父、子 Turn 完成并正常退出。已存父绑定没有子线程 ID，新 GX 冷启动另一个官方 App Server。
3. GX 从父历史里的 spawn 项找到子 ID。子历史给出相同父 ID、官方来源、确切原父 Turn 和有时间戳的终态。此时 GX 已核验子线程的计费归属。产品写权限仍归原 GX 根线程。单独标注的本地回调负例以这次真实子 ID、Turn ID 调用提交入口，得到 `SCOPE_DENIED`。
4. 配置的结算宽限期为 5 秒。真实 Driver 在宽限期之后产生一条 `budget.settled`，再次打开 Driver 后金额和事件 ID 完全相同。两次父请求、一次子请求均发生于原始操作，没有恢复请求、重复 Job 或新预算占用。

| 结果 | 历史 61ea96d | 候选 eb0cd52 |
|---|---|---|
| 官方冷读 | 父 spawn 和子终态均可见 | 父 spawn 和子终态均可见 |
| GX 对子线程的核验 | `metadata_verified=0`，子 Turn 用量表 0 行 | `metadata_verified=1`，子 Turn 归入原父 Turn |
| 命令结果 | 直接用例返回 1，未通过子归属断言 | 旧 supplemental 调用新用例，内外均返回 0 |
| 财务结果 | 轮询期间已有临时估算，核验仍失败 | 唯一结算事件 27，1000 tokens，`ESTIMATED` |

候选 Run 是 `gxr_01m87a928pmbrgwf23`，Job 是 `gxj_01m87a92eeas8bmf6n`。GX 捕获的用量下界为 35 tokens。测试线路单独记录了原始三个 Responses 的 253 tokens。GX 按原始授权额估算 1000 tokens，继续保留估算置信度与原始预约 `rsv_01m87a93qg104v1ztx`。从终止记录到估算出账实测为 5.002 秒。此次故障发生在 GX 与仍在运行的原生主机之间，丢帧、进程终止以及节点完成都有独立的时间记录。

## 原始记录和复现

- 候选原始请求、官方帧、丢失开始帧时间线、冷读回包、SQLite 压缩备份以及宽限期前后的账本均在 [候选证据](eb0cd52/supplemental/evidence/receipt.json)。[候选外层门禁](eb0cd52/supplemental/observation.json) 记录原始命令和内层返回码。[范围负例与计量测试](eb0cd52/scope-controls.log) 共 26 条通过。
- 历史对照只将候选的两个验收脚本复制进旧工作树，两边回执的脚本 SHA256 相同。[历史回执](baseline-61ea96d/evidence/receipt.json) 保留真实非零结果。[历史终态回包](baseline-61ea96d/evidence/cold-history.jsonl) 与 [历史 SQLite](baseline-61ea96d/evidence/ledger.sqlite.gz) 可交叉核对旧版本未建立已验证子 Turn 的原因。
- 在本目录执行 `sha256sum -c manifest.sha256` 可核验全部文件。已准备好依赖的发布目录支持 `bin/gx-acceptance run hardening --case CHILD-DOUBLE-LOSS`。旧门禁的完整启动方式见 [网络与启动记录](eb0cd52/network.json)。

公共入口也通过同一用例，自动打包、校验后为 PASS。这个运行在并行开发工作树进行，环境回执保留当时的 SHA 与文件差异。上表候选列取自独立冻结工作树。[公共入口结果](public-cli/child-gate/summary.json)

## 长临时目录复验

完整回归嵌套运行时，原夹具在绑定 Unix 套接字时因路径过长退出。版本 `096a82d` 为套接字单独创建 `/tmp` 下的私有短目录，工作库、Codex 状态和证据继续使用调用方指定的位置。旧 supplemental 和新用例入口在同一隔离网络命名空间内各实际执行一次。两次原路径等效长度均为 258 字节，实测新地址为 29 字节，退出后地址均已清理。两个原始 Job 各自只提交一次。额外注入读取线程启动异常，验证当时启动的官方主机已退出、stdin/stdout 已关闭。[长路径对照及异常清理记录](long-tmpdir/096a82d/result.json)

公共 `hardening` 入口也在长 TMPDIR 下通过，归档校验覆盖 22 份原始记录。[长路径公共入口](long-tmpdir/096a82d/public-cli/long-socket/summary.json)

## 继承的 v2.1 原生子线程回归

版本 `3940eb5` 以原 v2.1 执行器重跑完整子用量、缺失子用量、快速完成后冷读三个官方场景，三项退出码均为 0。冻结的断言名称全部保留，原始回执哈希逐一吻合。快速完成场景中，只读取线程摘要时，官方返回 `notLoaded` 和子线程自己的 `sessionId`。读取带 Turn 的父、子历史后，同时观测到原父 spawn 项、同一个子 ID、精确父来源和带完成时间的因果 Turn。GX 核验位从 0 变为 1。子用量仍未知，账本记录一次 100 tokens 的估算，没有调整或额外模型请求。五项范围负例和既有子线程读取回归通过。[三个官方场景](v21-3940eb5/suite/summary.json) [快速完成的官方历史](v21-3940eb5/suite/native/native-unloaded-child.json) [冻结断言比较](v21-3940eb5/frozen-comparison.json) [范围控制](v21-3940eb5/scope-controls.log)
