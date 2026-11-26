# 首航预测、实测与采纳

Run `gxr_01m8cgr15h0txt5abp`，Goal v1。实际结题决定事件 639，关闭事件 641。

## 原生事前登记的预测

| 登记 / 提交 / 端点首次可得事件 | 端点及范围 | 预测，实测 | 总段 / 合格 / 实测 / 缺测 / 遗漏 | GX 对此次登记的最后重评 | 实际调度者和决策引用 |
|---|---|---|---|---|---|
| 72 / 78 / 101 | 9VGW common_ca angle_deg · e=0.1°, z=0.02°, k=10 | missed_present 预期 1，实测 1 | 14 / 13 / 13 / 0 / 1 | preregistered / undetermined (out_of_scientific_scope) | Lead，委派 78，引用此标量 373。同作业其他标量 373 (blocks_eligible, blocks_evaluable, blocks_missed_observed, blocks_total, blocks_unmeasured), 500 (blocks_missed_observed), 639 (blocks_missed_observed)。仅引产物 440, 489 |
| 73 / 78 / 110 | 1VYM common_ca rmsd_A · e=0.05Å, z=0.01Å, k=10 | missed_present 预期 0，实测 1 | 14 / 13 / 13 / 0 / 1 | preregistered / undetermined (out_of_scientific_scope) | Lead，委派 78，引用此标量 373。同作业其他标量 373 (blocks_eligible, blocks_evaluable, blocks_missed_observed, blocks_total, blocks_unmeasured), 500 (blocks_missed_observed), 639 (blocks_missed_observed)。仅引产物 440, 489 |
| 366 / 373 / 465 | 1VYM common_backbone rmsd_A · e=0.05Å, z=0.01Å, k=10 | missed_present 预期 1，实测 1 | 14 / 13 / 13 / 0 / 1 | tested / supported | module:module_756544261b6a2ed986d9a23e3ee1bafd，委派 440，引用此标量 无。同作业其他标量 639 (blocks_missed_observed)。仅引产物 489 |
| 367 / 373 / 457 | 9VGW common_backbone angle_deg · e=0.1°, z=0.02°, k=10 | missed_present 预期 1，实测 1 | 14 / 13 / 13 / 0 / 1 | tested / supported | module:module_756544261b6a2ed986d9a23e3ee1bafd，委派 440，引用此标量 无。同作业其他标量 639 (blocks_missed_observed)。仅引产物 489 |
| 494 / 500 / 517 | 1VYM common_backbone angle_deg · e=0.1°, z=0.02°, k=10 | blocks_missed_observed 预期 3，实测 3 | 14 / 13 / 13 / 0 / 3 | tested / supported | module:module_756544261b6a2ed986d9a23e3ee1bafd，委派 489，引用此标量 639 |
| 495 / 500 / 526 | 9VGW common_backbone rmsd_A · e=0.05Å, z=0.01Å, k=10 | missed_present 预期 0，实测 1 | 14 / 13 / 13 / 0 / 1 | tested / opposed | module:module_756544261b6a2ed986d9a23e3ee1bafd，委派 489，引用此标量 639。同作业其他标量 639 (blocks_evaluable, blocks_missed_observed) |

## 关闭时的当前版本

事件 626–631 对同六条已有预测形成新版本，各条目的匹配端点均已可得。

| 已知结果时改版事件 | 原预测 | 当前版本 | 当前阶段及方向 |
|---|---|---|---|
| 626 | 495 | gxh_01m8cj2n8wdejy4m1y@4 | grounded / undetermined |
| 627 | 366 | gxh_01m8chjhhcmg51tkw1@5 | grounded / undetermined |
| 628 | 367 | gxh_01m8chjhjr2ztqsqn8@5 | grounded / undetermined |
| 629 | 494 | gxh_01m8cj2n6ftwmpjk9z@4 | grounded / undetermined |
| 630 | 72 | gxh_01m8cgyj3p5e2fvaee@6 | idea / undetermined |
| 631 | 73 | gxh_01m8cgyj4b6nr0zr1z@6 | idea / undetermined |

## 已完成作业与逐段证据

- `gxj_01m8cgyj9xm783vsha` · run · deposited-block-signed@1 · 委派事件 78。
  - 9VGW common_ca angle_deg · 1/13 段遗漏 · 产物 `gxa_01m8cgykgkb1ekzd2e`。
  - 101–120 attenuated · AB +0.098774，AC -0.136944，BC +0.041719，中位数 +0.098774。
- `gxj_01m8cgyjgpcbz9mdbd` · run · deposited-block-signed@1 · 委派事件 78。
  - 1VYM common_ca rmsd_A · 1/13 段遗漏 · 产物 `gxa_01m8cgykxfbdpv152n`。
  - 101–120 attenuated · AB +0.032938，AC -0.096095，BC -0.017289，中位数 -0.017289。
- `gxj_01m8ch30k7an8fnjb3` · module:module_76ba124d28334567ea398b11c335f9ae · deposited-chain-geometry@2 · 委派事件 162。
  - 1VYM ordered_backbone · 39/256 个位点，3/3 对可拟合。
- `gxj_01m8ch30shpx2t51jp` · module:module_76ba124d28334567ea398b11c335f9ae · deposited-chain-geometry@2 · 委派事件 162。
  - 9VGW ordered_backbone · 0/250 个位点，0/3 对可拟合。
- `gxj_01m8ch3aj1b4z8rpra` · module:module_932e17bb89d2854fc7de3f57901918fb · deposited-block-signed@1 · 委派事件 199。
  - 1VYM common_ca angle_deg · 3/13 段遗漏 · 产物 `gxa_01m8ch3bqw7xy9dj61`。
  - 61–80 near_zero · AB -0.125064，AC +0.114836，BC -0.012596，中位数 -0.012596。
  - 161–180 attenuated · AB -0.087414，AC +0.157690，BC -0.072995，中位数 -0.072995。
  - 181–200 attenuated · AB +0.333459，AC -0.303481，BC +0.069561，中位数 +0.069561。
- `gxj_01m8ch3aqrpeeqy0k7` · module:module_932e17bb89d2854fc7de3f57901918fb · deposited-block-signed@1 · 委派事件 199。
  - 9VGW common_ca rmsd_A · 0/13 段遗漏 · 产物 `gxa_01m8ch3byzf2bj9qbr`。
- `gxj_01m8ch50qcgcdtyzne` · module:module_76ba124d28334567ea398b11c335f9ae · deposited-chain-geometry@2 · 委派事件 250。
  - 1VYM common_ca · 255/256 个位点，3/3 对可拟合。
- `gxj_01m8ch50ydx04wszcv` · module:module_76ba124d28334567ea398b11c335f9ae · deposited-chain-geometry@2 · 委派事件 250。
  - 1VYM common_backbone · 255/256 个位点，3/3 对可拟合。
- `gxj_01m8ch514e90ng19jt` · module:module_76ba124d28334567ea398b11c335f9ae · deposited-chain-geometry@2 · 委派事件 250。
  - 9VGW common_ca · 241/250 个位点，3/3 对可拟合。
- `gxj_01m8ch51ae75tkchzj` · module:module_76ba124d28334567ea398b11c335f9ae · deposited-chain-geometry@2 · 委派事件 250。
  - 9VGW common_backbone · 241/250 个位点，3/3 对可拟合。
- `gxj_01m8chzkekebkgy012` · module:module_756544261b6a2ed986d9a23e3ee1bafd · deposited-block-signed@1 · 委派事件 440。
  - 1VYM common_backbone rmsd_A · 1/13 段遗漏 · 产物 `gxa_01m8chzq9ewhwtevm4`。
  - 101–120 attenuated · AB +0.025135，AC -0.113045，BC -0.025049，中位数 -0.025049。
- `gxj_01m8chzkpaystzt37m` · module:module_756544261b6a2ed986d9a23e3ee1bafd · deposited-block-signed@1 · 委派事件 440。
  - 9VGW common_backbone angle_deg · 1/13 段遗漏 · 产物 `gxa_01m8chzpwsmw4t2xzg`。
  - 101–120 attenuated · AB +0.078455，AC -0.134697，BC +0.059838，中位数 +0.078455。
- `gxj_01m8cj2neddntft3vc` · module:module_756544261b6a2ed986d9a23e3ee1bafd · deposited-block-signed@1 · 委派事件 489。
  - 1VYM common_backbone angle_deg · 3/13 段遗漏 · 产物 `gxa_01m8cj2rmqbd9c4js8`。
  - 61–80 near_zero · AB -0.123250，AC +0.112076，BC -0.011008，中位数 -0.011008。
  - 161–180 attenuated · AB -0.094148，AC +0.154901，BC -0.063035，中位数 -0.063035。
  - 181–200 attenuated · AB +0.329747，AC -0.310942，BC +0.081229，中位数 +0.081229。
- `gxj_01m8cj2nkxr6ca90xx` · module:module_756544261b6a2ed986d9a23e3ee1bafd · deposited-block-signed@1 · 委派事件 489。
  - 9VGW common_backbone rmsd_A · 1/13 段遗漏 · 产物 `gxa_01m8cj2sa1r165m6jq`。
  - 101–120 attenuated · AB -0.092304，AC -0.093187，BC -0.014727，中位数 -0.021243。
