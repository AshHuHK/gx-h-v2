# 多轮科研会话验收

基线为 `science/multi-workflow@4ea1f67`。网页和 CLI 共用原操作员收件箱、Codex Main 回合及回复记录。Main 负责对话和指代，Scientific Lead 与模块在各自冻结的科研简报上作决定。GX 用现有 Job、证据和决定记录显示 PI 原话的去向。[从空目录到网页](README.md)

| 范围 | 已核对的行为 | 证据 |
|---|---|---|
| A1 跨端会话 | Chromium 发、CLI 读；CLI 发、网页刷新后可见。丢失 POST 回执后重发只入账一次。重启网页及 Driver 后，浏览器按游标续读，原 Run 保持暂停。旧 Native 网页仍可收件。 | [浏览器与队列记录](evidence/controlled/observations.json) |
| A2 复合消息 | 问依据、只讨论、呈现偏好各有独立处置。单纯要求中文时，原本会被取消的三个科研任务保留身份与状态。定向材料只进入接收者的新简报；正式改题沿用原文授权与预览。 | [受控案例与复跑](evidence/controlled/README.md) |
| A3 澄清与记忆 | 两个未决问题可分别作答。等待 A 时 B 可完成 CPU Job。重启或更换物理 Main 后可找回旧 Lead 的公开理由；模块私有草稿不作为该理由。 | [问题与交接测试](../multiturn/test_pi_questions.py) · [线程恢复测试](../multiturn/test_main_replacement.py) |
| A4 探索与方法 | 新 Run 停滞后可补测、开模块、等待或有据结题。旧 Run、正式收敛及硬限均已验证。调研找到的已装方法在 1VYM 得到 33 个接触残基对，只有文献依据的 DSSP 方法保留缺口，新增该方法 Job 为零。 | [方法验收](../multi_workflow/test_multiturn_method_survey.py) |
| A5 封存与后继 | 跨越封存的旧预览不能写入。已错位的 CLOSED v2 表头仍可精确重导原 v1 报告。并发/重复请求只建一个后继，父题的已知端点进入新题时保留历史属性。 | [封存回归](../multiturn/test_sealed_intent.py) · [后继恢复](test_successor.py) |
| A6 官方宿主科研 | 两名研究者的正式 Codex 回合重叠 32.164 秒。PI 在交集内向 A 发质疑。A 采纳，B 在原路线交回自己的报告，Lead 引用两份报告结题。开放后继用新的 3TBL 原件自选局部测量并封存。14/14 项联合核验通过。 | [实跑回执](evidence/live/joint-checks.json) · [一页科学结论](evidence/science/SCIENCE.md) |

`82a087e` 的兼容、多轮及恢复三组分别通过 46、21、14 项。实跑发现的报告行号误判在 `9b36858` 修复。该构建再次通过 46 项兼容、17 项读数与报告、9 项受影响的后继及模块回归。另验证了 PCNA 来源与字段定位的正反例。两份既有科研档案均通过校验，其中保存的重导回执各记录新增 0 Job、0 宿主回合。[受控回执](evidence/controlled/README.md) · [首轮实跑与原请求回放](evidence/first-attempt/README.md)

## 两次实跑

| 科研构建 | 父题 → 同会话后继 | 签收 |
|---|---|---|
| `e2a102a` | `gxr_01m8d44vvx9cda854x` → `gxr_01m8d4w4vdg5r77603` | 两题均封存，13/14。B 的计算成功，但 GX 在两次拒收后代为生成部分报告。保留首次结果。[逐项结果](evidence/first-attempt/closure.json) |
| `9b36858` | `gxr_01m8d4v7h6x27e09s6` → `gxr_01m8d5edt69rah4ndn` | 两题均封存，14/14。A 在事件 175 采纳 PI 的质疑。父题关闭于事件 235，后继关闭于事件 311。[模型回合、两条路线及读数](evidence/live/joint-checks.json) |

两次均由已登录的官方 Codex 经 Clash 运行。四项研究共用三个公开结构，两次父题使用相同原件。重复观测用于核对软件、协作与再现性。首次项目在原部署路径升级后还做了锚定回读，Main 能从同一条旧消息引用当时的决定和报告，花费一个对话回合、零科研 Job；随后两份报告各自重导，计算与模型回合增量均为零。[升级后的真实问答及重导](evidence/first-attempt/sealed-readback.json)

修复版系列从启动到自动核验约 19 分钟，父题执行两项 CPU Job，后继执行一项。随后关掉模型，分别从普通 `research --run-id` 取回两份封存报告，新增量均为 0 Job、0 宿主回合。[父子独立取回记录](evidence/live/warm-exports.json)
