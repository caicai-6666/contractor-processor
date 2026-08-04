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

- `core`、`attribute`、`clause`、`abstract` 完整结果；
- 四类结果数量；
- 整份合同墙钟时间；
- `prepare`、`core`、`attribute`、`clause`、`abstract` 的开始偏移和运行时间，用于核对并行关系；
- 完整图执行窗口内所有节点按缓存查询量加权的文件级平均缓存命中率；
- 若失败，保存阶段名、正式门禁摘要和脱敏指标，不保存模型 raw response。

人工可见追溯使用 PDF 文件名，不展示 `document_id` 哈希。`summary.yaml` 只保存便于汇报的合同
摘要，完整业务结果留在对应 `contracts/*.yaml`。

---

## 分析记录

运行完成后，按 [`experiment-analysis-template.md`](../experiment-analysis-template.md) 在本次运行目录
维护只追加的 `analysis.md`；分析必须链接 YAML 原始产物，不能修改原结果。
