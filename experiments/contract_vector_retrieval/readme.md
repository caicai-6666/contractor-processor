# 合同四向量内存召回实验

> **定位：** 本实验先调用正式 production 提取图，再使用正式 Qwen3-VL Embedding 指令在内存中评估四类合同向量；不连接 Elasticsearch，也不执行合同或向量持久化。

## 实验流程

```text
data/input/*.pdf
  → 正式 Core / Attribute / Clause / Abstract 提取
  → 生成三类文本候选向量 + PDF 视觉候选向量
  → 构造 HyDE / 视觉变换 / 名称局部词 / 产品变体查询
  → 内存余弦排序
  → result.yaml + extraction/*.yaml
```

四类测试的查询策略固定为：

| 向量字段 | 查询构造 |
| --- | --- |
| `abstract_vector` | 将人工预置的假想合同摘要作为模拟 HyDE 文档向量化；同时保留原始用户问题 |
| `document_visual_vector` | 多页 PDF 删除最后一页；单页 PDF 旋转 90°并做非等比缩放 |
| `contract_name_vector` | 使用合同正式名称中的简写或部分关键词 |
| `product_names_vector` | 使用俗称、简称、型号或产品名称的一部分 |

测试查询集中维护在 [`cases.yaml`](cases.yaml)，每条查询在运行前指定唯一目标 PDF。修改测试集会改变实验口径，应同步提升 `schema_version`，不能在观察排名后静默改题。

## 输出与指标

每次运行生成：

- `status.yaml`：运行状态与副作用边界；
- `extraction/*.yaml`：正式完整提取结果；
- `result.yaml`：候选向量输入文本、全部查询的完整排序及四个向量字段的汇总指标；
- `analysis.md`：运行完成后的人工分析记录。

汇总指标包括 `Recall@1`、`Recall@3`、MRR、目标平均相似度，以及目标相对最高错误候选的平均分差。报告不保存 2048 维原始向量，避免产物膨胀。

> **评价边界：** 当前只有 5 份合同且每条查询只标注一个相关目标。指标可比较四个向量在本批次的区分能力，但不能代表更大合同库中的最终召回率。

## 运行方式

```bash
python -u experiments/contract_vector_retrieval/run.py --input-dir data/input
```

若提取已成功而后续实验代码失败，可复用该运行的已校验提取产物，避免再次调用 MLLM：

```bash
python -u experiments/contract_vector_retrieval/run.py \
  --input-dir data/input \
  --reuse-extraction-run experiments/outputs/contract_vector_retrieval/<run-id>
```

脚本末尾提供 `IDE_INPUT_DIR`、`IDE_CASES`、`IDE_OUTPUT_DIR` 与 `IDE_MAX_DOCUMENTS`，可以直接从 IDE 启动。测试用例集合默认必须与输入 PDF 集合完全一致，防止候选集或标注集被静默截断。

## 依赖与设计决策

- 提取复用 `build_process_contract()`，不复制正式字段提示词或校验逻辑；
- 向量化复用 `Qwen3VLEmbeddingClient` 与 `contract_embedding.yaml`；
- 视觉派生 PDF 只存在于临时目录，查询完成后删除；
- 排名由实验内存中的余弦相似度完成，不依赖 Elasticsearch 近似 KNN 参数；
- 实验不会把自动提取结果伪装为专家终审结果，也不会调用正式入库用例。
