# 独立坐标复核

Run `gxr_01m8cftps3hcwzxvz3`，Goal 状态 `CLOSED`。只复核了此 Run 中已成功完成并发布的物理 Job。

独立路径从原始 mmCIF 的模型 1、实体 1 和标签链 A/B/C 读取坐标，按已执行 Job 的原子选择进行正旋转 SVD 拟合。省略区段后先对三条链对分别重拟合，随后取各阶段的中位数。角度或 RMSD 的带符号变化均为删后减删前。

## 2RE9 · deposited-block-influence@1 · `gxj_01m8cfxbxqmefskkmw`

原件 `2RE9`，`common_backbone`，合格区段 8 个。所有基线、逐区段和分散对照拟合均已独立重算。

- `angle_deg` 的最大链对变化为 0.612723 degree，链对 AC，标签区段 101–120。
- `rmsd_A` 的最大链对变化为 0.252804 angstrom，链对 AC，标签区段 21–40。

## 2O0O · deposited-block-influence@1 · `gxj_01m8cfxc4j2a2bd1pq`

原件 `2O0O`，`common_backbone`，合格区段 8 个。所有基线、逐区段和分散对照拟合均已独立重算。

- `angle_deg` 的最大链对变化为 0.391912 degree，链对 AB，标签区段 21–40。
- `rmsd_A` 的最大链对变化为 0.329986 angstrom，链对 BC，标签区段 21–40。

## 2RE9 · deposited-block-influence@1 · `gxj_01m8cfxf6vy37j6r6y`

原件 `2RE9`，`common_ca`，合格区段 8 个。所有基线、逐区段和分散对照拟合均已独立重算。

- `angle_deg` 的最大链对变化为 0.594142 degree，链对 AC，标签区段 101–120。
- `rmsd_A` 的最大链对变化为 0.224322 angstrom，链对 AC，标签区段 21–40。

## 2O0O · deposited-block-influence@1 · `gxj_01m8cfxfdqpsb78xp9`

原件 `2O0O`，`common_ca`，合格区段 8 个。所有基线、逐区段和分散对照拟合均已独立重算。

- `angle_deg` 的最大链对变化为 0.357730 degree，链对 BC，标签区段 21–40。
- `rmsd_A` 的最大链对变化为 0.296237 angstrom，链对 BC，标签区段 21–40。

## 2RE9 · deposited-chain-geometry@2 · `gxj_01m8cfxfmys73hvbaw`

全链 `ordered_backbone` 选择覆盖 49/164 个观测位置。角度（°）与 RMSD（Å）按相同匹配原子独立重拟合。

| 链对 | 旋角（°） | RMSD（Å） |
|---|---:|---:|
| AB | 120.168257 | 0.123675 |
| AC | 120.112834 | 0.121847 |
| BC | 119.721126 | 0.130487 |

## 2O0O · deposited-chain-geometry@2 · `gxj_01m8cfxfw84f13yb1v`

全链 `ordered_backbone` 选择覆盖 26/153 个观测位置。角度（°）与 RMSD（Å）按相同匹配原子独立重拟合。

| 链对 | 旋角（°） | RMSD（Å） |
|---|---:|---:|
| AB | 120.234417 | 0.283305 |
| AC | 120.008364 | 0.284435 |
| BC | 119.758271 | 0.293639 |

## 2RE9 · deposited-block-reading@1 · `gxj_01m8cfzqczse8k2zhs`

所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。

## 2RE9 · deposited-block-reading@1 · `gxj_01m8cfzqkdkx4qwdqr`

所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。

## 2O0O · deposited-block-reading@1 · `gxj_01m8cfzqt8r1dk98ea`

所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。

## 2O0O · deposited-block-reading@1 · `gxj_01m8cfzr0qtgj46nag`

所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。

## 2O0O · deposited-block-reading@1 · `gxj_01m8cfzr75hy2704s3`

所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。

## 正式结题

事件 404 采纳决定，事件 406 关闭 Goal。该决定显式引用 29 项已交付指标，29 项与独立复算相符。原 Job、原值、单位及复算差值保留在 JSON。

[完整参数、输出摘要与逐区段读数](results.json)
