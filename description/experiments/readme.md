# 快速实验区

> **定位：** 项目根目录的 `experiments/` 是个人开发阶段的验证工作区；本目录只提供实验说明、入口索引与复现边界。

实验目录不属于正式模块，不得被 `src/` 依赖。Core、Clause、Abstract、终审入库与 Attribute 空策略
的现有 `run.py` 是调用正式服务的薄入口；新 Demo 验证成功后也应将可复用实现迁入
`src/`，并为稳定行为补充 `tests/`。详细规则与模板见
[`../../experiments/readme.md`](../../experiments/readme.md)。

> **迁移原则：** Demo 验证成功后，应将可复用实现迁入 `src/`，并为稳定行为补充 `tests/`；实验目录不能成为生产代码依赖。

---

## 实验索引

| 主题 | 主说明 | 用途 |
| --- | --- | --- |
| Core | [Core 字段提取](core-extraction.md) | 正式异步 Core 服务回归 |
| Clause | [Clause 三阶段提取](clause-extraction.md) | 结构发现、边界与原文提取回归 |
| Attribute | [空节点实验](../../experiments/attribute_extraction/readme.md) | 验证空目录的明确语义 |
| Abstract | [摘要生成](../../experiments/contract_summary_generation/readme.md) | 正式摘要服务回归 |
| 完整处理 | [production 批量回归](../../experiments/contract_processing_batch/readme.md) | Core、Attribute、Clause、Abstract 全流程批处理 |
| 终审 Mock | [待入库包络](../../experiments/contract_ingestion_mock/readme.md) | 审核包络与输入契约验证 |
| 终审入库 | [四节点入库验收](contract-ingestion.md) | PDF、检索投影、多向量与实验索引写入 |
| 视觉检索 | [自查询召回](contract-visual-retrieval.md) | 视觉向量链路一致性 |
| 视觉鲁棒性 | [变换合同召回](contract-visual-robustness.md) | 删页、旋转和缩放下的稳定性 |
| 字段发现 | [第一大步与组级收敛](field-discovery-group-consolidation.md) | 统一流水线与历史复现入口 |

> **分析记录：** 需要解释某次运行时，在该运行目录的 `analysis.md` 中追加结论和证据；不要改写原始 JSON、日志或报告。
