# 已沉积蛋白与环肽的质控对照

1QYS 的 Top7 有 106 个声明残基，实验模型在 A3–94 有 92 个完整主链。6BEN 的 20 个 NMR 模型均有可定位的首尾原子。文件真实保存了末位 GLN8 的 C 至首位 DAR1 的 N 的共价连接。这些结论可由归档原件复算。[原件、下载时间与 SHA-256](sources.json)

| 对照 | 实测 | 解释 |
| --- | --- | --- |
| 1QYS 坐标 | 92/106 个位置可见，92 个 Cα。Cα 回转半径 12.216062 Å | 分母包括未解析的 1–2、95–106。以 wwPDB PDB 的固定列和成对平方距离独立计算，与 mmCIF 的质心法一致。 |
| 6BEN 原件 | 四个端点原子映射成功 20/20 个模型。模型 1 的 C–N 为 1.335140 Å，两侧键角为 118.836°、123.386° | 保留 DAR、DPR、DGN、DGL 的沉积化学类型。20 个构象共享一次 NMR 沉积。 |
| 6BEN 缺键反例 | 显式闭环声明从 1 变为 0，20 组端点距离与两侧夹角均与原件相同 | 唯一修改是删除 `_struct_conn` 的 `covale2` 行。验证器逐字节检查其余文件。端点接近本身不能证明文件描述了成键。 |
| 6BEN 重原子检查 | 20 个模型中合计 0/32,240 个指定非相邻残基重原子对相距小于 2 Å | 比较时排除同位、顺序相邻以及环首尾两残基。该阈值只描述短距离，不是能量或稳定性判据。 |

坐标插件为 `org.gx.deposited-structure-qc@1.0.0`，提供 `deposited-backbone-qc@1`、`deposited-macrocycle-qc@1`。每次使用一个名为 `structure` 的已作用域绑定的 mmCIF 输入，无方法参数。产物 `qc.json` 可在 Goal 中引用，包含原件哈希、范围、整数或小数读数。环肽方法支持单链、首尾 C–N、最多 64 个残基。多链、替代构象或微异质位置需要使用其他方法。解读缺键反例时须标明它的衍生材料身份。

## 离线复算

在仓库根目录执行以下命令。配套 PDB 文件为同一沉积的另一种格式，复算独立性来自解析和数学实现。文件仅证实这两份沉积的表现。脚本同时验证 20 组闭环判别、坐标映射、键角和逐模型非相邻对分母。[机器可读结果](verification.json)

```bash
bin/gx-p0 plugin verify org.gx.deposited-structure-qc@1.0.0
.venv/bin/python -B acceptance/science_capabilities/structure_controls/verify.py
```

## 固定 Top7 对照的 H800 正向预测

已用既有 AlphaFold 3 在 H800 实跑一次公开 Top7 的 92 位点标准氨基酸序列，映射到实验 A3–94。实验 A37 为 MSE，对应预测输入中的 Met。预测器只收到序列、空 paired/unpaired MSA 与空模板，关闭数据搜索，seed 1，1 个样本，10 次 recycle。坐标拟合在预测完成后执行。原生模型推理为 97.29 秒。92 个 Cα 上的 RMSD 为 **1.011876 Å**，平均 Cα pLDDT 为 **88.419**。SVD 与独立四元数法相差约 1.24×10⁻¹⁴ Å。单个旧沉积可能已在训练数据中，本次校准的解释限于这条已知对照序列。此 GPU 校准有独立回执，GX 业务 Run 及 Lead 交付另计。[固定输入与运行记录](native_prediction/run.json) · [原始模型坐标](native_prediction/published_top7_1qys_model.cif) · [AF3 输出使用条款](native_prediction/TERMS_OF_USE.md)

既有 AF3 checkout 的 `docs/input.md` 的 Bonds 一节明确指出，未支持 polymer 内部的自定义共价键。本次未用线性 AF3 代替 6BEN 的环拓扑预测。历史 GX 的 `cyclic-design-20261109/MODEL_COMPATIBILITY.md` 和 `operational-handoffs.json` 将 HighFold4 的已验能力记录为输入准备。环型预测仍缺在本部署上经过已知阳性校准的构象输出。已发布的插件能力限于原件测量。[锁定版 AF3 输入说明](https://github.com/google-deepmind/alphafold3/blob/a66cc5226d5fbcf7b08af4495af6fd7262178e38/docs/input.md#bonds)

上游数据来自 wwPDB，原件及各自论文见 [1QYS](https://www.rcsb.org/structure/1QYS) 与 [6BEN](https://www.rcsb.org/structure/6BEN)。
