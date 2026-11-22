# 2RE9 坐标几何敏感性研究

从发布包根目录运行下列命令。样例把研究范围限定在 TL1A 已沉积的晶体坐标，最多两轮、四个 CPU Job。项目状态保存在指定目录。

```bash
./bin/gx-p0 start --state ./var/tl1a --spec examples/tl1a_geometry/project.json
./bin/gx-p0 run --state ./var/tl1a
./bin/gx-p0 status --state ./var/tl1a
```

`start --state ./var/tl1a --tl1a-geometry` 使用同一个示例。用 `stop --state ./var/tl1a` 暂停驱动，用 `resume --state ./var/tl1a` 继续。Job 与已完成的独立工作都从该项目的 SQLite 和原始产物恢复。

新项目默认运行 `tl1a_geometry@2`。继续已有项目时，GX 读取其保存的精确版本。需要重现首版行为的新项目可在 spec 中指定 `"workflow_version": "1"`。

研究从目标和来源开始。GX 先执行坐标盘点 Job，随后在同一项目内执行独立版本化的 `tl1a_coordinate_quality@1` 工作流，产出三种可选原子集合及各自的可观测残基覆盖率。几何工作流的规划员读取这些结果，选择适合研究问题的路线，提出可用数据区分的预测，并冻结覆盖率、角度和 RMSD 的比较界限与复算容差。每个几何 Job 的输入 manifest 都包含原始 mmCIF、冻结准则及质量工作流实际发布的原子集合。

如果项目已有评判准则，可把下列 `criteria` 对象放入项目 JSON。它会成为主对话中版本化的 `evaluation` 条目，规划员必须逐项遵守。这里的数值仅用于展示格式。

```json
{
  "coverage": {
    "min_residues": 60,
    "min_fraction": 0.6,
    "denominator": "observed_union_residues"
  },
  "comparison": {
    "max_angle_delta_deg": 0.5,
    "max_rmsd_delta_angstrom": 0.15
  },
  "reproduction": {
    "angle_abs_tol_deg": 0.01,
    "rmsd_abs_tol_angstrom": 0.001
  }
}
```

版本 2 的科研简报给出原始坐标的 SHA-256、七个实际字节偏移与相应原文片段。可先读质量报告与片段，再按具体问题用 `gx_read_artifact` 请求相应页。评审简报列出当轮每条预测的短 ID 与规范 ID。每条预测任选其中一个，只提交一次。局限性可用一段文字或多条非空文字，GX 逐条保留原文。

独立 reviewer 通过 `gx_recalculate_geometry` 对分配到的路线重新解析原始坐标，以 Biopython 和 NumPy 的 SVD 配准核对实际 Job 的 Horn 四元数结果。复核产物包含每个链对的数值、原子对应、阈值和差异。可观测覆盖不足或复算不一致时保留原始数据，评审可以给出证据不足。决策员根据评审说明哪些证据与预测相符、剩余不确定性，以及是否追加一个尚未测量的原子集合。改变评价准则时，GX 保留原始测量和原标准，独立标记回顾性重评，复用已有 Job。

报告限于该次沉积中 A、B、C 三条链的观测位置。有关溶液态组装、共同对称轴、结合能力和药物活性的结论需要各自的独立实验。[项目示例](project.json) · [坐标来源与原文定位](../../gx-h/adapters/scientific/tl1a_geometry/data/provenance.json) · [方法与参考实现](../../gx-h/adapters/scientific/tl1a_geometry/README.md)
