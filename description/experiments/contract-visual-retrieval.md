# 合同视觉向量召回实验

> **定位：** 本实验验证已入库合同的 PDF 视觉向量能否正确自查询召回；它不接入自动提取主图，也不修改 Elasticsearch 文档。

---

## 模块用途

`experiments/contract_visual_retrieval/` 验证已入库合同的 PDF 视觉向量自查询召回。它不属于
自动提取 LangGraph 主图，也不写入、删除或重建 Elasticsearch 文档。

---

## 主要职责

- 从明确指定且已完成的 Mock 运行读取成功合同、源 PDF 与 `document_id`；
- 复用正式入库模块的 Qwen3-VL-Embedding 页面渲染、逐页向量化和归一化平均融合策略；
- 仅通过 `document_visual_vector` 执行 LlamaIndex VectorStore KNN 查询；
- 为每份合同记录自身排名、分数、完整候选序列和视觉页数；
- 汇总 Rank-1 自查询召回、候选集 Recall 与 MRR；
- 校验索引全部 `_id` 与 Mock 成功合同集合完全一致，防止候选污染。

> **候选集门禁：** 评估前必须确认索引 `_id` 集合与本次成功 Mock 合同完全一致，否则排名指标没有解释意义。

---

## 关键设计决策

当前测试集的查询 PDF 与入库 PDF 相同，所以这是一项链路一致性测试：它能发现错误的渲染、
模型、融合、维度、索引字段或 KNN 配置，但不能代表近重复合同判重能力。判重评估应额外构造
同一合同的压缩、删页、换页、重排和重扫版本，并要求原合同在 Top-K 中出现。

实验复用 `Qwen3VLEmbeddingClient.embed_pdf()` 与
`Elasticsearch9ContractVectorStore.aquery()`，因此与入库时的视觉 embedding 指令、144 DPI 渲染、
`normalized_page_mean_v1` 融合和 ES 9 KNN 调用保持一致。排名评估只读取检索返回的 ID、分数
和源文件名，不保存高维向量。

> **结论边界：** 查询 PDF 与入库 PDF 相同，结果只证明渲染、模型、融合、向量维度、索引字段与 KNN 链路一致；不能据此声明具备近重复判重能力。

---

## 使用方式与依赖

入口为 `experiments/contract_visual_retrieval/run.py`，要求 `--mock-run` 参数。它依赖：

- 已完成且与索引严格匹配的 `contract_ingestion_mock` 运行；
- 已完成入库的独立实验索引；
- Qwen3-VL-Embedding 服务和 Elasticsearch 9 HTTPS 配置。

完整命令、输出目录和解释边界见
[`experiments/contract_visual_retrieval/readme.md`](../../experiments/contract_visual_retrieval/readme.md)。

> **后续验证：** 判重能力需要额外构造压缩、删页、换页、重排与重扫样本，并检验原合同是否仍出现在 Top-K。
