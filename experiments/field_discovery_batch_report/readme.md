# 字段发现批次精简汇报实验

> **定位：** 本实验调用正式 `DiscoverFieldsFromBatch` 两阶段模块，但只输出阶段汇报需要的字段
> 定义、命中率、批次耗时和批次平均缓存命中率。

---

## 运行

```bash
python experiments/field_discovery_batch_report/run.py --input-dir data/input
python experiments/field_discovery_batch_report/run.py --max-documents 1
```

IDE 手动启动时可修改 `run.py` 末尾的 `IDE_*` 变量；命令行显式参数优先。

---

## 输出

每次运行只维护两个 YAML：

```text
experiments/outputs/field_discovery_batch_report/<run_id>/
├── status.yaml
└── result.yaml
```

`result.yaml` 包含：

1. `fields[]`：通过收敛和全局门禁的完整字段定义；
2. `fields[].statistics`：命中合同数、总合同数、命中率和命中 PDF 文件名；
3. `batch.wall_clock_seconds`：从缓存前快照到结果完成的批次墙钟时间；
4. `batch.average_cache_hit_rate_percent`：整个字段发现批次内，以 vLLM prefix cache 查询量加权的
   平均缓存命中率。

命中率使用 `found_document_count / distinct_document_count`，即正式 DTO 的
`conservative_frequency`；技术失败仍留在总分母中，不能通过缩小分母抬高汇报值。缓存计数不可用
时明确输出 `null` 和测量原因，不伪造为 0%。

该精简文件不复制候选池、关系边、逐模型调用或逐合同完整提取包；正式 discovery 结果仍按既有
边界写入 `data/definitions/discovery/result/<batch_id>.yaml`。

---

## 分析记录

若实际运行，必须在同一运行目录按 [`experiment-analysis-template.md`](../experiment-analysis-template.md)
追加 `analysis.md`，并链接 `result.yaml`，不得修改原始 YAML。
