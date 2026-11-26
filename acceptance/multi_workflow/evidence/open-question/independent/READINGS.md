# 独立坐标复核

Run `gxr_01m8ck7s94gn2qcw5q`，Goal 状态 `CLOSED`。只复核了此 Run 中已成功完成并发布的物理 Job。

独立路径从原始 mmCIF 的模型 1、实体 1 和标签链 A/B/C 读取坐标，按已执行 Job 的原子选择进行正旋转 SVD 拟合。省略区段后先对三条链对分别重拟合，随后取各阶段的中位数。角度或 RMSD 的带符号变化均为删后减删前。

## 3JA9 · deposited-chain-geometry@2 · `gxj_01m8ckeknzfngq05tb`

全链 `common_ca` 选择覆盖 255/256 个观测位置。角度（°）与 RMSD（Å）按相同匹配原子独立重拟合。

| 链对 | 旋角（°） | RMSD（Å） |
|---|---:|---:|
| AB | 120.899791 | 1.388408 |
| AC | 119.583103 | 1.055476 |
| BC | 119.943330 | 1.314243 |

## 3JA9 · deposited-chain-geometry@2 · `gxj_01m8ckekwbzm3bx1g7`

全链 `common_backbone` 选择覆盖 255/256 个观测位置。角度（°）与 RMSD（Å）按相同匹配原子独立重拟合。

| 链对 | 旋角（°） | RMSD（Å） |
|---|---:|---:|
| AB | 120.894917 | 1.375952 |
| AC | 119.573404 | 1.084418 |
| BC | 119.956598 | 1.312199 |

## 3JA9 · deposited-chain-geometry@2 · `gxj_01m8ckem33e81jmbvt`

全链 `ordered_backbone` 选择覆盖 39/256 个观测位置。角度（°）与 RMSD（Å）按相同匹配原子独立重拟合。

| 链对 | 旋角（°） | RMSD（Å） |
|---|---:|---:|
| AB | 120.998441 | 0.363449 |
| AC | 119.411159 | 0.376599 |
| BC | 120.182231 | 0.245759 |

## 3TBL · deposited-chain-geometry@2 · `gxj_01m8ckema0pxs53v4z`

全链 `common_ca` 选择覆盖 249/253 个观测位置。角度（°）与 RMSD（Å）按相同匹配原子独立重拟合。

| 链对 | 旋角（°） | RMSD（Å） |
|---|---:|---:|
| AB | 119.942329 | 0.787174 |
| AC | 119.752958 | 0.544458 |
| BC | 120.310635 | 0.784343 |

## 3TBL · deposited-chain-geometry@2 · `gxj_01m8ckemfz6rgke2wg`

全链 `common_backbone` 选择覆盖 249/253 个观测位置。角度（°）与 RMSD（Å）按相同匹配原子独立重拟合。

| 链对 | 旋角（°） | RMSD（Å） |
|---|---:|---:|
| AB | 119.975272 | 0.857075 |
| AC | 119.762251 | 0.651733 |
| BC | 120.265785 | 0.851900 |

## 3TBL · deposited-chain-geometry@2 · `gxj_01m8ckemqfap4h2k6t`

全链 `ordered_backbone` 选择覆盖 0/253 个观测位置。角度（°）与 RMSD（Å）按相同匹配原子独立重拟合。

| 链对 | 旋角（°） | RMSD（Å） |
|---|---:|---:|
| AB | 缺测 | 缺测 |
| AC | 缺测 | 缺测 |
| BC | 缺测 | 缺测 |

## 3JA9 · deposited-block-signed@1 · `gxj_01m8ckey2f6wagjj0c`

原子选择 `common_backbone`，指标 `angle_deg`（deg）。阈值 e=0.1、z=0.02，最少可评价区段 k=10。合格 13/14 段，可评价 13 段，其中 3 段属遗漏。独立计算 missed_present=1。

全部逐段重算的最大绝对差为单次拟合 7.11e-14 deg，变化量 6.95e-14 deg。对应容差为 0.01 和 0.02 deg。分类和数值存在 0 项超差或不一致。

| 省略标签区段 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 判定 |
|---|---:|---:|---:|---:|---|
| 61–80 | -0.123267 | +0.112112 | -0.011026 | -0.011026 | near_zero |
| 161–180 | -0.094162 | +0.154898 | -0.063017 | -0.063017 | attenuated |
| 181–200 | +0.329743 | -0.310923 | +0.081216 | +0.081216 | attenuated |

## 3TBL · deposited-block-signed@1 · `gxj_01m8ckey8x8dhseknt`

原子选择 `common_backbone`，指标 `angle_deg`（deg）。阈值 e=0.1、z=0.02，最少可评价区段 k=10。合格 13/14 段，可评价 13 段，其中 1 段属遗漏。独立计算 missed_present=1。

全部逐段重算的最大绝对差为单次拟合 5.68e-14 deg，变化量 7.77e-14 deg。对应容差为 0.01 和 0.02 deg。分类和数值存在 0 项超差或不一致。

| 省略标签区段 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 判定 |
|---|---:|---:|---:|---:|---|
| 161–180 | -0.167895 | +0.033795 | +0.135040 | -0.167895 | opposed |

## 3JA9 · deposited-block-signed@1 · `gxj_01m8ckeyf92593d61w`

原子选择 `common_backbone`，指标 `rmsd_A`（Å）。阈值 e=0.05、z=0.01，最少可评价区段 k=10。合格 13/14 段，可评价 13 段，其中 1 段属遗漏。独立计算 missed_present=1。

全部逐段重算的最大绝对差为单次拟合 4.22e-15 Å，变化量 6.64e-15 Å。对应容差为 0.001 和 0.002 Å。分类和数值存在 0 项超差或不一致。

| 省略标签区段 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 判定 |
|---|---:|---:|---:|---:|---|
| 101–120 | +0.025136 | -0.113034 | -0.025044 | -0.025044 | attenuated |

## 3TBL · deposited-block-signed@1 · `gxj_01m8ckeyp8zq481atf`

原子选择 `common_backbone`，指标 `rmsd_A`（Å）。阈值 e=0.05、z=0.01，最少可评价区段 k=10。合格 13/14 段，可评价 13 段，其中 1 段属遗漏。独立计算 missed_present=1。

全部逐段重算的最大绝对差为单次拟合 1.22e-15 Å，变化量 1.94e-15 Å。对应容差为 0.001 和 0.002 Å。分类和数值存在 0 项超差或不一致。

| 省略标签区段 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 判定 |
|---|---:|---:|---:|---:|---|
| 181–200 | -0.031500 | -0.061946 | -0.043789 | -0.043789 | attenuated |

## 3JA9 · deposited-block-signed@1 · `gxj_01m8ckpfj96y6jtgks`

原子选择 `common_ca`，指标 `angle_deg`（deg）。阈值 e=0.1、z=0.02，最少可评价区段 k=10。合格 13/14 段，可评价 13 段，其中 3 段属遗漏。独立计算 missed_present=1。

全部逐段重算的最大绝对差为单次拟合 8.53e-14 deg，变化量 1.14e-13 deg。对应容差为 0.01 和 0.02 deg。分类和数值存在 0 项超差或不一致。

| 省略标签区段 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 判定 |
|---|---:|---:|---:|---:|---|
| 61–80 | -0.125076 | +0.114900 | -0.012649 | -0.012649 | near_zero |
| 161–180 | -0.087445 | +0.157750 | -0.073026 | -0.073026 | attenuated |
| 181–200 | +0.333465 | -0.303478 | +0.069561 | +0.069561 | attenuated |

## 3TBL · deposited-block-signed@1 · `gxj_01m8ckpfsmdcbfj2m5`

原子选择 `common_ca`，指标 `angle_deg`（deg）。阈值 e=0.1、z=0.02，最少可评价区段 k=10。合格 13/14 段，可评价 13 段，其中 2 段属遗漏。独立计算 missed_present=1。

全部逐段重算的最大绝对差为单次拟合 5.68e-14 deg，变化量 8.16e-14 deg。对应容差为 0.01 和 0.02 deg。分类和数值存在 0 项超差或不一致。

| 省略标签区段 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 判定 |
|---|---:|---:|---:|---:|---|
| 81–100 | +0.132484 | -0.029034 | -0.101982 | +0.132484 | opposed |
| 161–180 | -0.185346 | +0.041183 | +0.145525 | -0.148188 | opposed |

## 3JA9 · deposited-block-signed@1 · `gxj_01m8ckpg1akvjfc7fa`

原子选择 `common_ca`，指标 `rmsd_A`（Å）。阈值 e=0.05、z=0.01，最少可评价区段 k=10。合格 13/14 段，可评价 13 段，其中 2 段属遗漏。独立计算 missed_present=1。

全部逐段重算的最大绝对差为单次拟合 4.22e-15 Å，变化量 8.05e-15 Å。对应容差为 0.001 和 0.002 Å。分类和数值存在 0 项超差或不一致。

| 省略标签区段 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 判定 |
|---|---:|---:|---:|---:|---|
| 101–120 | +0.032938 | -0.096091 | -0.017288 | -0.017288 | attenuated |
| 141–160 | +0.050000 | +0.034087 | +0.044729 | +0.044729 | attenuated |

## 3TBL · deposited-block-signed@1 · `gxj_01m8ckpg8kt2dk0098`

原子选择 `common_ca`，指标 `rmsd_A`（Å）。阈值 e=0.05、z=0.01，最少可评价区段 k=10。合格 13/14 段，可评价 13 段，其中 0 段属遗漏。独立计算 missed_present=0。

全部逐段重算的最大绝对差为单次拟合 1.78e-15 Å，变化量 1.6e-15 Å。对应容差为 0.001 和 0.002 Å。分类和数值存在 0 项超差或不一致。

| 省略标签区段 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 判定 |
|---|---:|---:|---:|---:|---|
| 121–140 | -0.094234 | -0.035899 | -0.112685 | -0.112685 | represented |

## 3JA9 · deposited-block-influence@1 · `gxj_01m8ckpgf1qtghm19j`

原件 `3JA9`，`common_backbone`，合格区段 13 个。所有基线、逐区段和分散对照拟合均已独立重算。

- `angle_deg` 的最大链对变化为 0.329743 degree，链对 AB，标签区段 181–200。
- `rmsd_A` 的最大链对变化为 0.392153 angstrom，链对 AB，标签区段 181–200。

## 3TBL · deposited-block-influence@1 · `gxj_01m8ckz3r75xnz52sx`

原件 `3TBL`，`common_backbone`，合格区段 13 个。所有基线、逐区段和分散对照拟合均已独立重算。

- `angle_deg` 的最大链对变化为 0.167895 degree，链对 AB，标签区段 161–180。
- `rmsd_A` 的最大链对变化为 0.109269 angstrom，链对 BC，标签区段 121–140。

## 3JA9 · deposited-block-reading@1 · `gxj_01m8cme3yf5vq9wsfw`

所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。

## 3JA9 · deposited-block-reading@1 · `gxj_01m8cme45jt0vzzgfv`

所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。

## 3TBL · deposited-block-reading@1 · `gxj_01m8cmq8769hb3ym83`

所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。

## 3TBL · deposited-block-reading@1 · `gxj_01m8cmq8dxs6p3cc3z`

所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。

## 3TBL · deposited-block-reading@1 · `gxj_01m8cmq8mewkk1fk4s`

所测指标、范围及复算结果见 JSON 中该 Job 的 evidence 字段。

## 同一区段的原子选择复核

逐个已执行 signed Job 计，共有 13 个遗漏区段读数。其中 13 个可在同一原件、同一区段、同一指标上与另一原子选择匹配，0 个匹配区段缺测，0 个缺少另一原子选择的对应 Job。

| 原件 · 区段 | 指标 | 原子选择 | AB 变化 | AC 变化 | BC 变化 | 三对中位变化 | 该 Job 签名分类 |
|---|---|---|---:|---:|---:|---:|---|
| 3JA9 · 61–80 | angle_deg | common_backbone | -0.123267 | +0.112112 | -0.011026 | -0.011026 | near_zero |
| 3JA9 · 61–80 | angle_deg | common_ca | -0.125076 | +0.114900 | -0.012649 | -0.012649 | near_zero |
| 3JA9 · 161–180 | angle_deg | common_backbone | -0.094162 | +0.154898 | -0.063017 | -0.063017 | attenuated |
| 3JA9 · 161–180 | angle_deg | common_ca | -0.087445 | +0.157750 | -0.073026 | -0.073026 | attenuated |
| 3JA9 · 181–200 | angle_deg | common_backbone | +0.329743 | -0.310923 | +0.081216 | +0.081216 | attenuated |
| 3JA9 · 181–200 | angle_deg | common_ca | +0.333465 | -0.303478 | +0.069561 | +0.069561 | attenuated |
| 3TBL · 161–180 | angle_deg | common_backbone | -0.167895 | +0.033795 | +0.135040 | -0.167895 | opposed |
| 3TBL · 161–180 | angle_deg | common_ca | -0.185346 | +0.041183 | +0.145525 | -0.148188 | opposed |
| 3JA9 · 101–120 | rmsd_A | common_backbone | +0.025136 | -0.113034 | -0.025044 | -0.025044 | attenuated |
| 3JA9 · 101–120 | rmsd_A | common_ca | +0.032938 | -0.096091 | -0.017288 | -0.017288 | attenuated |
| 3TBL · 181–200 | rmsd_A | common_backbone | -0.031500 | -0.061946 | -0.043789 | -0.043789 | attenuated |
| 3TBL · 181–200 | rmsd_A | common_ca | -0.028172 | -0.043993 | -0.032507 | -0.032507 | quiet |
| 3JA9 · 61–80 | angle_deg | common_backbone | -0.123267 | +0.112112 | -0.011026 | -0.011026 | 仅有原变化量 |
| 3JA9 · 161–180 | angle_deg | common_backbone | -0.094162 | +0.154898 | -0.063017 | -0.063017 | 仅有原变化量 |
| 3JA9 · 181–200 | angle_deg | common_backbone | +0.329743 | -0.310923 | +0.081216 | +0.081216 | 仅有原变化量 |
| 3TBL · 81–100 | angle_deg | common_ca | +0.132484 | -0.029034 | -0.101982 | +0.132484 | opposed |
| 3TBL · 81–100 | angle_deg | common_backbone | +0.117216 | -0.022881 | -0.093646 | +0.117216 | represented |
| 3TBL · 81–100 | angle_deg | common_backbone | +0.117216 | -0.022881 | -0.093646 | +0.117216 | 仅有原变化量 |
| 3TBL · 161–180 | angle_deg | common_backbone | -0.167895 | +0.033795 | +0.135040 | -0.167895 | 仅有原变化量 |
| 3JA9 · 101–120 | rmsd_A | common_backbone | +0.025136 | -0.113034 | -0.025044 | -0.025044 | 仅有原变化量 |
| 3JA9 · 141–160 | rmsd_A | common_ca | +0.050000 | +0.034087 | +0.044729 | +0.044729 | attenuated |
| 3JA9 · 141–160 | rmsd_A | common_backbone | +0.049624 | +0.035522 | +0.044295 | +0.044295 | quiet |
| 3JA9 · 141–160 | rmsd_A | common_backbone | +0.049624 | +0.035522 | +0.044295 | +0.044295 | 仅有原变化量 |

表中每行对应一个实际 Job。各 Job 的阈值、分母及对应关系见 JSON 的 matched_masks。

## 正式结题

事件 662 采纳决定，事件 664 关闭 Goal。该决定显式引用 26 项已交付指标，26 项与独立复算相符。原 Job、原值、单位及复算差值保留在 JSON。

[完整参数、输出摘要与逐区段读数](results.json)
