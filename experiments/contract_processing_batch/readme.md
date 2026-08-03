# 合同完整处理批量回归实验

> **定位：** 该入口以 `production` 模式逐份运行 Core、Attribute、Clause 与 Abstract，并保存每份完整终审候选，用于批量回归。

---

## 运行

```bash
python experiments/contract_processing_batch/run.py --input-dir data/input
```

---

## 输出与失败处理

实验会写入 `batch_manifest.json`、增量 `summary.json` 与每份成功合同的 `<序号>_result.json`。
若某阶段未通过正式门禁，额外写入 `<序号>_failure_diagnostic.json`，其中只含阶段名、校验
对象和内存指标，不含模型 raw response。失败合同仍继续处理后续文件。`ProcessContract.execute()`
的资源生命周期是一份合同一次，因此实验按合同重建 use case；正式 `run_batch` CLI 只输出摘要，
不承担实验落盘。

> **诊断边界：** 失败诊断只保存阶段名、校验对象和内存指标，不保存模型原始响应；正式批量 CLI 不落盘。

---

## 分析记录

实验完成后，人工分析必须在同一运行目录新建或追加 `analysis.md`，不修改原始 JSON 产物。

> **记录原则：** 分析结论应链接该目录内的 manifest、结果或诊断产物，并与原始 JSON 分离维护。
