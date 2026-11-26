# P4、K7 与公开坐标研究验收

**两份公开沉积可用同一套质控程序。** 先检查三个链对。三对同向且中位数反映共同变化时可汇总。出现方向相反、个别链对超限或局部敏感性达到预定条件时，分列链对、区段、指标和原子选择。[科研报告](PUBLIC_CHAIN_PAIR_QC.md) · [逐对读数与独立复算](numerical/README.md)

两个诊断按首次分派计分。已启动 **2/2**，科研终态 **2/2**，读后首提获采纳 **1/2**，PI 正式签收 **1/2**。三项 Run 均使用官方 OpenAI `gpt-6.2-astra`、推理强度 `max`。事件号按行内 Run 读取。

| Run 与实际运行提交 | 真实回合与 CPU 作业 | 送达、决定与终态 |
|---|---|---|
| **P4 单层诊断**<br>`gxr_01m8b4p5mcvbcfz193`<br>`2ec22a595e0aa6276f7704b60a7b392e685b4a1d` | Lead 5 回合。标本 Job `gxj_01m8b4tek33j9ppef4`、参照 Job `gxj_01m8b4wxs9bh8yas2q` 均成功。 | 34 预定门槛 3/5。76 送达 **149/164**，82 首提获采纳，维持比较。92 参照 Job 成功，测得 **164/164**。114 采纳结题，116 关闭。用量 649,964 tokens，PI **PASS**。 |
| **K7 委派诊断**<br>`gxr_01m8b56krznt1nr8zn`<br>`2ec22a595e0aa6276f7704b60a7b392e685b4a1d` | Lead 4 回合、子模块 2 回合。子模块 Job `gxj_01m8b5bbp5hsa2eyzf` 成功。 | 35 预定门槛 3/5。106 发布报告，107 唤醒 Lead，116 送达 **54/164**。119、120、121 提交被退回，129 第四次提交生效，撤回比较，131 关闭。PI 认可停止理由，正式签收 **HOLD**。中断回合总量未知，已收到回执的下界为 628,733 tokens。 |
| **2RE9、2O0O 普通研究**<br>`gxr_01m8b7pxvpathjd78y`<br>`5846848ae9cf17de24ad07c79ed90e0ddb80d30d` | Lead 5 回合。4 个区间筛查、6 个链对读数 Job 全部成功。 | 47 选法并启动筛查，96、98 送达端点。114、185 各追加 4 个复核 Job。242 起送达最后一批结果。279 的结题提交被退回，修正后 283 请求关闭，**288** 采纳引用 **52 个端点**的最终决定，**290** 关闭 Goal。Run 终态 `STOPPED`。 |

**普通研究的 10 个 Job** 按实际委派次序分三批，可凭完整 ID 对照源产物。

- 事件 47 启动 `gxj_01m8b7w270hzjcjryt`、`gxj_01m8b7w2fgrmn3znw1`。
- 事件 114 启动 `gxj_01m8b8a08qq4va8s44`、`gxj_01m8b8a0fkstf6q69s`、`gxj_01m8b8a0prbmhjf5pz`、`gxj_01m8b8a0wmtbmaz4np`。
- 事件 185 启动 `gxj_01m8b8n4mrsxgatfcv`、`gxj_01m8b8n4vevc51s3kb`、`gxj_01m8b8n51r2yn437bg`、`gxj_01m8b8n58bga8pb9jt`。

Goal v1 的最终决定为 `gxa_4cc4d23f41dad450aaafb877a2cd59cb6e25133f44c368a674116a49cae997a0`。52 个显式引用均匹配原送达记录。结题后从同一入口重新打开 Run，复用了原有的 10 次作业尝试和 5 个模型回合，前后均为 293 条事件。[最终决定与引用](final-goal-decision.json) · [同 Run 复用回执](warm-reopen.json)
