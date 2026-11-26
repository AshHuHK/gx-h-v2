# 同一科研问题的并行调查

从仓库根目录运行，使用已部署的 GX 环境、Codex 登录和科学插件。正式问题使用两份公开人 PCNA 原始沉积。Lead 选择预测、掩码、物理量和阈值，两位调查者共享一个 Goal，各有自己的分支。方法的单位、输入、输出和局部比较的范围见[方法卡](methods.json)。

| 研究 | 材料和已知信息 | 问题与授权配置 |
|---|---|---|
| 旧材料彩排 | 2RE9、2O0O，已有参考计算 | [双路线问题卡](rehearsal.md) · [配置](rehearsal.json) |
| 正式首航 | 1VYM、9VGW，只用覆盖筛查入组。[原件与资格](data/qualification.json) | [首航问题卡](first-flight.md) · [配置](goal.json) |
| 自主选路 | 3JA9、3TBL，另批入组的原件。[原件与资格](open-data/qualification.json) | [开放问题卡](open-question.md) · [配置](open.json) |

```bash
./bin/gx-p0 research --state ./runs/rehearsal --run-key pcna-rehearsal-01 \
  --goal-file examples/pcna/rehearsal.md --goal-config examples/pcna/rehearsal.json --research-effort medium

./bin/gx-p0 research --state ./runs/pcna --run-key pcna-first-01 \
  --goal-file examples/pcna/first-flight.md --goal-config examples/pcna/goal.json --research-effort medium

./bin/gx-p0 research --state ./runs/open --run-key pcna-open-01 \
  --goal-file examples/pcna/open-question.md --goal-config examples/pcna/open.json --research-effort medium
```

重复原命令会继续同一 Run。结题后该命令从原始决定和读数导出 `REPORT.md`、`result.json`，终端给出实际路径。研究中途可用 `./bin/gx-p0 status --state <原目录>` 查看进度。要替换材料，复制配置文件，为每份原件给出相对路径、SHA-256、取得记录，并为这项新研究选择新的 run key。数据保留各自的条目版本与沉积编号。[坐标来源与许可](https://www.wwpdb.org/about/usage-policies)

入组前已计算的覆盖与区段数属于已知事实。首航将有符号局部遗漏端点留至 Lead 登记预测后测量。开放题提供方法目录，Lead 自选方法、预测和是否委派。同题内链对共享链，两种原子选择共享结构与拟合实现。推断范围限于给定的沉积坐标。同一材料的重放可验证复现性。
