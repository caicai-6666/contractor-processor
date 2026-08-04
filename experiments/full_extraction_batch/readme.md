# 完整合同提取批量功能测试

> **定位：** 本实验批量调用正式 production `ProcessContract`，完整执行 Core、Attribute、Clause
> 和 Abstract，不以候选字段回扫代替完整提取。

---

## 正式并行拓扑

实验不自行编排四类算法，而是使用正式 LangGraph：

```text
prepare
  ├─ core → attribute ─┐
  ├─ clause ───────────┼→ finalize
  └─ abstract ─────────┘
```

Core、Clause、Abstract 在共享 PDF 准备完成后并行开始；Attribute 必须等待 Core 的字段结果和合同
理解地图。各分支共享页面、多模态客户端和模型请求限流器。批次层按合同串行，避免多份完整 PDF
同时占满本地 vLLM。

---

## 运行

```bash
python experiments/full_extraction_batch/run.py --input-dir data/input
python experiments/full_extraction_batch/run.py --max-documents 1
```

IDE 手动启动时可修改 `run.py` 末尾 `IDE_*` 变量；显式命令行参数始终优先。

---

## 输出

每次运行写入 `experiments/outputs/full_extraction_batch/<run_id>/`：

```text
<run_id>/
├── manifest.yaml
├── summary.yaml
└── contracts/
    ├── 01_<合同文件名>.yaml
    └── ...
```

每份合同 YAML 同时包含：

- `core`、`attribute`、`clause`、`abstract` 结果；Attribute 字段在一次纠错后仍失败时只省略
  该字段，其他成功字段保留；
- 四类结果数量；
- `processing.attribute_extraction` 中的 Attribute 完整/局部状态、跳过字段和脱敏失败诊断；
- 整份合同墙钟时间；
- `prepare`、`core`、`attribute`、`clause`、`abstract` 的开始偏移和运行时间，用于核对并行关系；
- 完整图执行窗口内所有节点按缓存查询量加权的文件级平均缓存命中率；
- 若 Core、Clause 或 Abstract 硬失败，保存阶段名、正式门禁摘要和脱敏指标；Attribute 局部
  失败仍生成合同 YAML，并在处理诊断中列明。所有路径均不保存模型 raw response。

人工可见追溯使用 PDF 文件名，不展示 `document_id` 哈希。`summary.yaml` 只保存便于汇报的合同
摘要，并通过 `batch.partial_attribute_contract_count` 单独统计 Attribute 局部成功合同；完整业务
结果留在对应 `contracts/*.yaml`。存在局部成功时批次状态为 `completed_with_failures`，但该合同
仍计入 `succeeded_contract_count`，不会混入整份失败数。

---

## 分析记录

运行完成后，按 [`experiment-analysis-template.md`](../experiment-analysis-template.md) 在本次运行目录
维护只追加的 `analysis.md`；分析必须链接 YAML 原始产物，不能修改原结果。
