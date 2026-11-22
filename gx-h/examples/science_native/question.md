# 合成评分实验

研究问题是，正区间 [0, 3] 和负区间 [−3, 0] 的哪些采样点能达到 −0.25 分？请用实际本地计算检验竞争解释，评估有限采样能支持的结论，并决定下一轮应测哪里。所有结论限定在这个合成评分函数和已测网格。

本次方法为 `fixture-grid@1`。它在本地 CPU 上对每个有限数值 x 计算 `score(x) = -(x - 2)^2`，输出每个 candidate_id、x 和 score。它只评价给定网格上的合成样本。请由 Root 根据此定义提出可被反驳的预测。在看到输出之前，为每条路线写出预测、支持标准和可推翻它的观测，选择有区分力的采样点，并在预测文字里解释选择理由。共同判据是该路线至少一个观测值达到 −0.25 分。评估所有观测均低于阈值时的含义，以及未覆盖区间带来的限制。Root 决定路线和网格，最多执行两轮。

通过 `gx_run_start` 注册主目标，使用 `science_grid@1`。下面这份完整请求演示一个单点方法检查的输入格式。实际运行时将 objective、paths 中的预测和候选点替换为为本研究设计的内容。使用项目驱动给出的 project_ref 和原始幂等键。

```json
{
  "project_ref": "grid-science",
  "idempotency_key": "gx-project-objective:grid-science",
  "budget_profile": "cpu_only",
  "objective": "Check whether a single calibration point reaches the prespecified threshold.",
  "workflow": {"id": "science_grid", "version": "1"},
  "inputs": {
    "max_rounds": 2,
    "proposal": {
      "threshold": -0.25,
      "paths": [
        {
          "id": "calibration",
          "prediction": "The calibration point reaches -0.25. A lower observed value refutes this point prediction.",
          "grid": {
            "synthetic": true, "method_id": "fixture-grid", "method_version": "1",
            "candidates": [{"candidate_id": "calibration-0", "x": 0.0}]
          }
        }
      ]
    }
  }
}
```

研究开始后，保留返回的 run ID。GX 根据已冻结的 proposal 运行计算和收集回执。在有就绪科研任务时，Root 按实际任务要求分派原生 worker。独立 evidence_reviewer 读取 proposal、逐路线原始观测和执行状态，引用产物并说明每条预测得到何种支持。再由另一位 planner 阅读评估、权衡反证、选择有信息量的下一轮网格或解释停止理由。项目恢复时沿用已有的 run、已完成 Job 和评估。最终汇报各路线的观测范围、结果、反证、局限和下一轮决定，附可取回的 GX 原件引用。

## GX 预算

| 资源 | 上限 |
| --- | --- |
| job_submissions | 6 |
| cpu_seconds | 240 |
| gpu_jobs | 0 |
| gpu_minutes | 0 |
| worker_tasks | 8 |
