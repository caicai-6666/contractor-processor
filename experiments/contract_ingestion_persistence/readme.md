# 正式终审入库子图验收实验

本实验消费 `contract_ingestion_mock` 生成的终审包络，调用 `src/` 中正式
`IngestReviewedContract` 四节点 LangGraph。实验目录不再维护清洗、Embedding 或 ES
VectorStore 的第二套实现；同名模块仅提供兼容导出。

## 管线

```text
终审 Mock + PDF
  → Prepare
  ├─ Text Embedding ───┐
  └─ Visual Embedding ─┤
                       → Persist（PDF + ES）
```

PDF 保存至配置的 `data/contracts/<document_id>.pdf`。ES 写入完整终审回显对象、稀疏
Core/Attribute 投影、SmartCN 中文字段和四个独立向量。

## 索引重建规则

每次运行都必须先删除并重建目标索引，以保证候选集只包含本次 Mock：

- 目标必须包含 `experiment` 安全标记；
- 目标不得等于正式 `elasticsearch.index_name`；
- 索引存在时完整删除，然后按正式 mapping 重建；
- 不接受跳过重建的运行参数。

`clear_index.py` 和 `rebuild_empty_index.py` 仍保留为单独维护工具，但正常入库验收不需要先手工
调用它们。

## 运行

```bash
python experiments/contract_ingestion_persistence/run.py \
  --mock-run experiments/outputs/contract_ingestion_mock/<run-id>
```

运行产物位于 `experiments/outputs/contract_ingestion_persistence/<run-id>/`：

- `manifest.json`：索引删除/重建证据、逐合同状态和端到端强校验；
- `NN_ingestion_result.json`：清洗计数、PDF 回执、向量字段和页数；
- 失败诊断：保留 LangGraph 失败节点与原始异常类型。

强校验通过 `source_exclude_vectors=False` 取回 ES 向量，逐项确认维度、PDF 存储键与 SHA-256、
ES 文档数/唯一 ID，以及 Core/Attribute 稀疏投影不存在 null 或空容器。

## 当前非目标

- 不提供 FastAPI 路由；
- 不执行 VL 重复精判或 Reranker；
- 不建立 `contract_id` 和新旧版本替换关系；
- 不删除正式索引或正式业务数据。

## 最新验证

2026-08-03 最终运行 `20260803T020132810771Z`：索引先删除后重建，最新 5 份 Mock 全部成功，
ES 为 5 个唯一文档；合同名称、产品名称、摘要和 PDF 视觉四类向量均为 2048 维，对方公司
向量不存在，5 个 PDF 哈希和全部稀疏投影均通过强校验。完整分析见该运行目录的
`analysis.md`。
