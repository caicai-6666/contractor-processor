# Clause 抽取实验

> **定位：** 这是正式异步 Clause 服务的回归包装器，用真实 PDF 验证结构发现、边界规划与原文抽取的完整链路。

---

## 用途

`experiments/clause_extraction/run.py` 用于以真实 PDF 回归正式异步 Clause 服务。正式算法仍
包含结构发现、非重叠边界计划、逐候选复核、逐单元原文抽取及重复/包含硬校验。

---

## 使用方式

```bash
python experiments/clause_extraction/run.py --pdf data/input/example.pdf
```

通过正式阶段门禁的 payload 保存到
`experiments/outputs/clause_extraction/<run-id>/result.json`。正式 pipeline 不保存 Prompt、
Schema、原始响应、指标或失败报告。

> **产物边界：** 只有通过正式阶段门禁的 payload 会写入 `result.json`；诊断数据由实验包装层负责，正式 pipeline 不落盘。

---

## 设计决策

- 模型请求全部使用 `AsyncOpenAI`；
- 阶段 validation 通过 `StageResult` 在内存传递；
- 单项失败隔离和最终完整性门禁保留；
- 新的调试输出只允许在本实验包装层开发，迁入正式服务时必须移除；
- 人工分析按模板追加 `analysis.md`。

> **完整性门禁：** 单项失败可被隔离以保留诊断，但重复、包含关系与最终完整性校验仍必须全部通过，才能形成正式结果。

Clause 业务定义见 [Clause 字段说明](../fields/clause/clause.md)，正式运行边界见
[正式抽取服务](../capabilities/extraction-services.md)。
