# 终审合同入库实验模块

> **定位：** 本实验以最新终审 Mock 验收正式 `IngestReviewedContract` 四节点图；核心实现位于 `src/`，实验层只负责输入、实验索引重建和证据输出。

---

## 模块用途

本模块用最新终审 Mock 验收正式 `IngestReviewedContract` 四节点 LangGraph：无意义字段清洗、
关键字段文本向量化、PDF 全页视觉向量化、PDF 内容寻址落盘和 Elasticsearch 单文档多向量
写入。核心实现已经位于 `src/`，实验目录只负责输入、索引重建和证据输出。

---

## 主要职责

- 严格校验 Mock 包络、审核追溯信息与 PDF SHA-256；
- 根据最终 `value` 生成完整清洗对象和稀疏值投影；
- 用配置的我方公司别名排除我方，形成对方公司名称集合；
- 通过 Qwen3-VL Chat Embeddings 分别生成三个文本向量；
- 对 PDF 所有页面分别生成视觉向量，再融合为合同级视觉向量；
- 通过自定义 LlamaIndex VectorStore 使用 Elasticsearch 9 官方异步客户端写入；
- PDF 以 `data/contracts/<document_id>.pdf` 原子落盘并复核哈希；
- 写入后按 `_id + ingestion_attempt_id` 回读，随后强制取回向量验证维度。

> **索引安全门禁：** 实验只能操作带 `experiment` 标记的索引，且维护命令默认预览；实际执行必须同时提供 `--execute` 与完整索引名确认。

---

## 对外接口与节点

`IngestReviewedContract.execute(confirmation, source_pdf)` 调用独立图：

1. `prepare_ingestion()`；
2. `embed_text_fields()`；
3. `embed_pdf_visual()`；
4. `persist_contract()`。

文本与视觉节点在 Prepare 后并行，Persist 显式等待两者。自动提取 LangGraph 主图只在逻辑
流程图上连接“专家审核 → 入库”，不得直接调用该图。

> **图边界：** 自动提取主图只表达“专家审核 → 入库”的业务连接；实际写入必须通过这个独立图，不能绕过审核门禁。

每次正常验收运行会先删除并重建带 `experiment` 标记的目标索引，且明确拒绝正式索引，保证
ES 候选集只包含本次 Mock。独立维护入口仍包括 `clear_index.py` 和
`rebuild_empty_index.py`。前者只删除文档并保留 mapping；后者只允许删除并重建已经为零文档
的实验索引，使 analyzer 等创建期 mapping 变更生效。两者默认均为预览，实际执行必须使用
`--execute` 并通过 `--confirm-index-name` 完整确认目标，同时拒绝正式索引和不含
`experiment` 的索引名。重建入口还会先设置写阻断并二次计数，避免并发写入竞态。

---

## 关键实现与设计决策

### 双投影存储

ES `_source.reviewed_result` 保存清洗后的完整终审结构并设置 `enabled: false`，避免动态包络造成
mapping explosion。`core_values`、`attribute_values`、合同名称、对方名称、产品名称、Clause
和 Abstract 另行作为检索投影。这样同时满足终审结果回显和空值不污染 `exists`/检索语义。

稳定的中文 `text` 投影——源文件名、合同名称、对方名称、产品名称、Clause 标题与原文、
Abstract 文本——都显式使用 `smartcn` 作为索引和查询 analyzer。`core_values` 与
`attribute_values` 因开放字段结构使用 `flattened`，不具备 SmartCN 子字段分析能力；需要
开放字段中文全文检索时应另建汇总 `text` 投影。

### 多向量单文档

每份合同只写一条 ES 文档，四个向量字段用途固定：`contract_name_vector`、
`product_names_vector`、`abstract_vector` 和 `document_visual_vector`。对方公司名称仍写入
`counterparty_names` SmartCN 文本元数据，但不生成 dense vector。自定义 VectorStore 使用
LlamaIndex `TextNode` 作为写入对象，并用
`VectorStoreQuery.embedding_field` 选择检索向量字段。由于官方 LlamaIndex Elasticsearch
插件依赖 ES Python Client `<9`，本项目不能安装该插件或降低 ES 版本。

Elasticsearch 9 默认可能不在普通 `_source` 响应中返回 dense vector；KNN 索引中的向量仍然
存在。验证实际维度时必须显式传递 `source_exclude_vectors=False`，不能因为普通 GET 未展示
向量就误判为写入失败。

> **向量验证：** 写入成功不能只凭普通 GET 判断。维度校验必须显式请求返回向量，并同时核对目标 `_id` 与 `ingestion_attempt_id`。

### 多页视觉融合

当前策略为 `normalized_page_mean_v1`：144 DPI 全页渲染、逐页 Qwen3-VL-Embedding、页向量
归一化、等权平均和结果再次归一化。它适合第一阶段近重复 Top-K 召回，但会弱化单页差异，
因此不能替代后续 VL 精判。正式采用前必须构造相同内容重压缩、删除一页、替换一页、页面
重排和重新扫描样本，评估 Recall@K 与误召回分布。

### 索引所有权

实验写入管线允许创建不存在的独立索引，但不执行隐式迁移。mapping 不兼容、主分片不可用
或磁盘水位阻止分配时快速失败。独立维护入口仅在操作者显式确认、目标带实验标记且文档数
为 0 时允许重建索引；它不修改集群水位，也不触碰正式索引。

> **所有权边界：** 实验管线可创建独立实验索引，但绝不隐式迁移 mapping、调整集群水位或触碰正式索引。

---

## 依赖与配置

- `llama-index-core>=0.14,<0.15`；
- `elasticsearch>=9.4,<10.0`；
- 与 Elasticsearch 服务版本严格一致的 `analysis-smartcn` 插件；
- Qwen3-VL-Embedding-2B vLLM Chat Embeddings 服务；
- PyMuPDF 页面渲染；
- `.env` 中的 ES 凭据、Embedding API key 和
  `CONTRACT_PROCESSOR_OWN_COMPANY_NAMES`；
- `configs/settings.yaml` 中 2048 维 Embedding 配置及
  `elasticsearch.ingestion_experiment_index_name`。

具体命令、输出文件与索引安全规则见
[`experiments/contract_ingestion_persistence/readme.md`](../../experiments/contract_ingestion_persistence/readme.md)。

---

## 当前限制

正式模块尚不执行业务合同判重、Reranker、VL 精判、版本替换或正式 API 暴露；实验也不删除
正式索引。跨介质失败优先保证 ES 不引用缺失 PDF，孤儿 PDF 的离线回收仍是后续运维能力。

> **未实现范围：** 业务判重、Reranker、VL 精判、版本替换和正式 API 尚未进入该正式模块；实验结果不能视为这些能力已交付。

---

## 验证状态

2026-08-03 最终运行 `20260803T020132810771Z` 使用真实 Qwen3-VL-Embedding-2B 与 ES 9.4.4：
索引先删除后重建，最新 5 份 Mock 全部成功，ES 恰有 5 个唯一文档；每份合同名称、产品
名称、摘要和 PDF 视觉四个向量均强制取回并确认 2048 维，且不存在对方公司向量。
Core/Attribute 稀疏投影无空值，5 个 PDF 哈希全部与 document_id 一致；当前全量单元测试为
183 passed。完整分析见该运行目录的 `analysis.md`。

> **验证范围：** 上述结论仅对应所列运行与当时的依赖版本；后续变更应在新的运行目录复验，并追加分析记录。
