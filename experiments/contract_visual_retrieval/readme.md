# 合同视觉向量召回实验

本实验只评估已入库合同的 `document_visual_vector`。它从指定的、已完成的
`contract_ingestion_mock` 运行读取成功合同清单，对每份源 PDF 重新进行逐页视觉向量化与
合同级融合，再以该向量在 Elasticsearch 中对所有候选合同执行 KNN 查询。

## 测量内容

当前 5 份测试合同构成完整候选集。每份 PDF 依次回答：重算后的视觉向量能否召回其自身、
自身排第几、对应分数是多少，以及前 5 名候选是谁。

它是视觉向量链路的自查询基线，不是合同近重复判重的最终效果：查询 PDF 与索引中的 PDF
字节相同，因此 Rank-1 只能证明渲染、Embedding、融合、索引和 KNN 之间的一致性。后续应以
重压缩、删页、换页、重扫等派生版本作为查询集，评估“同一业务合同”是否仍在 Top-K。

## 候选集门禁

实验拒绝在候选集合不明确时给出排名。启动后会比较：

- 指定 Mock 运行的全部成功 `document_id`；
- 指定 Elasticsearch 索引的全部 `_id`。

二者必须完全相等。这样历史文档、漏入库合同或错误索引不会污染“5 份合同中的排名”。当前
查询只使用 `document_visual_vector`，没有关键词过滤、文本向量、重排模型或大模型判定。

## 运行

必须显式指定与当前索引对应的 Mock 运行：

```bash
python experiments/contract_visual_retrieval/run.py \
  --mock-run experiments/outputs/contract_ingestion_mock/20260802T132616923404Z
```

可用 `--index-name` 指向非默认实验索引。运行不会写入或删除 Elasticsearch 文档；只会向本地
Embedding 服务发起请求，并将评估产物写入
`experiments/outputs/contract_visual_retrieval/<run-id>/`。

## 输出

- `manifest.json`：候选集、模型、每份合同状态，以及 `rank_1_recall`、全候选集 Recall 和 MRR；
- `NN_visual_retrieval.json`：每份合同的视觉页数、自身 rank、倒数排名与完整候选排序；
- `NN_failure_diagnostic.json`：单合同渲染、Embedding 或查询失败；
- `runtime_failure_diagnostic.json`：索引候选集不一致、模型不可用等批次级失败。

高维向量和渲染页面都不会落盘。若需求方要求分析某次运行，须按
[`experiment-analysis-template.md`](../experiment-analysis-template.md) 在该运行目录追加
`analysis.md`。
