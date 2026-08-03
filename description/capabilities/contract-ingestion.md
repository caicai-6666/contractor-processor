# 专家终审合同入库模块

> **适用范围：** 本文说明专家确认后的 PDF、稀疏元数据和多向量入库链路；业务判重、版本替换与 HTTP 服务仍由后续能力承担。

---

## 模块用途

本模块在专家完成合同审核后，接收完整终审包络和对应原始 PDF，通过一张独立的四节点
LangGraph 保存 PDF、稀疏检索元数据和四类向量。它与 production/discovery 合同提取主图
物理解耦，未来 FastAPI 路由只负责解析请求并调用 `IngestReviewedContract.execute()`。

> **调用边界：** 自动提取只生成待审核候选；只有专家确认后的完整包络与原始 PDF 才能调用本用例写入正式存储。

---

## 工作流

```text
终审包络 + 原始 PDF
  → Prepare Ingestion
  ├─ Text Embedding ───┐
  └─ Visual Embedding ─┤
                       → Persist Contract
                          ├─ PDF 原子落盘
                          └─ Elasticsearch 单文档多向量写入
```

四个图节点分别为：

1. `prepare_ingestion`：校验内外层身份和 PDF SHA-256，生成终审稀疏投影；
2. `embed_text_fields`：并发生成合同名称、产品名称和摘要向量；
3. `embed_pdf_visual`：逐页生成视觉向量并按 `normalized_page_mean_v1` 融合；
4. `persist_contract`：保存 PDF、组装 ES 文档、经 LlamaIndex 写入并按 `_id` 回读校验。

第二、三个节点在图中并行，分别写入 `text_vectors` 和 `visual_vector` 状态键；第四个节点只有
在两个分支均完成后才运行。Embedding 失败不会产生 PDF 或 ES 写入。

> **写入前置条件：** 两条向量分支均完成后才允许持久化；任一 Embedding 失败必须阻止 PDF 和 Elasticsearch 外部写入。

---

## 对外接口

- `IngestReviewedContract.initialize()`：探测 Embedding 模型并创建或校验索引；
- `IngestReviewedContract.execute(confirmation, source_pdf)`：执行一次完整入库；
- `IngestReviewedContract.close()`：关闭 Embedding 与 Elasticsearch 异步客户端；
- `build_ingest_reviewed_contract(project_root, ...)`：为未来 API、Worker 和实验组装同一个用例；
- `LocalSourceDocumentStore.resolve(document_id)`：校验哈希后返回原始 PDF 路径。

未来 HTTP 接口应传入 `ContractReviewConfirmation` JSON 和 PDF 文件，不得在路由中复制清洗、
向量化或落盘逻辑。

---

## PDF 存储协议

`configs/settings.yaml` 的 `paths.source_documents` 默认指向 `data/contracts`。文件固定保存为：

```text
data/contracts/<document_id>.pdf
```

存储适配器先在目标目录创建临时文件，流式复制并 `fsync`，重新计算 SHA-256 后再通过同目录
原子替换激活。目标已存在且哈希正确时返回幂等成功；目标内容不一致时视为存储损坏并拒绝
覆盖。ES 只保存相对 `storage_key`、MIME 类型和字节数，不保存服务器绝对路径。

跨文件系统与 Elasticsearch 无法形成单一事务，因此采用“PDF 先成功、ES 后写入”的安全
顺序，保证 ES 不会指向缺失文件。ES 明确失败时保留内容寻址 PDF 作为可幂等复用的孤儿文件，
不在并发场景中冒险自动删除；后续可按 ES 引用关系提供独立垃圾回收任务。

> **幂等与安全：** PDF 先原子激活，ES 后写入。ES 失败时允许保留可复用孤儿文件，不能为“清理”而冒险删除可能被并发请求复用的内容。

---

## Elasticsearch 与向量

正式 mapping 使用 `dynamic: strict`，一份合同以 `document_id` 同时作为 `_id`，保存：

- `reviewed_result`：清洗后的完整终审包络，`enabled: false`；
- `core_values`、`attribute_values`：不含 null/空容器的稀疏检索投影；
- Clause、Abstract 和稳定中文全文检索字段；
- `contract_name_vector`、`product_names_vector`、`abstract_vector`、
  `document_visual_vector`；
- `counterparty_names` 作为 SmartCN 文本元数据保留，不生成 dense vector；
- PDF 存储元数据、审核追溯、向量策略和入库时间。

自定义 `Elasticsearch9ContractVectorStore` 使用 LlamaIndex `TextNode` 和 ES 9 官方异步客户端，
LlamaIndex 类型不穿透应用端口。写入响应不作为唯一成功证据：每次写入生成
`ingestion_attempt_id`，随后按 `_id` 回读并核对该 ID 与 PDF 存储键。若网络错误发生在 ES
实际提交之后，回读命中本次 attempt ID 时按成功处理。

向量指令和视觉策略位于 `data/definitions/contract_embedding.yaml`，索引文档记录其内容哈希
`instruction_version`。修改指令、模型、维度或融合策略会改变向量空间，必须重新向量化并通过
新索引版本迁移，不能静默混写。

> **向量版本边界：** 指令、模型、维度或视觉融合策略变化时，必须新建向量空间并重建索引；同一索引不能混写不同策略的向量。

---

## 配置与依赖

- `paths.contract_embedding_policy`：向量指令策略；
- `paths.source_documents`：PDF 持久化根目录；
- `ingestion.own_company_names_env`：保存我方公司名称 JSON 数组的环境变量名；
- `models.embedding`：vLLM Embedding 地址、模型、并发和维度；
- `elasticsearch.index_name`：正式索引；
- `elasticsearch.number_of_shards/number_of_replicas`：索引创建参数。

`elasticsearch.vector_dimensions` 必须与 `models.embedding.dimensions` 一致，配置加载阶段即
失败关闭。中文字段依赖与 ES 服务版本一致的 `analysis-smartcn` 插件。

---

## 故障与幂等语义

- 包络/PDF 哈希不一致：Prepare 失败，不调用 Embedding；
- 文本或视觉向量失败：Persist 不运行，不产生外部写入；
- PDF 同 ID、同内容：复用已有文件；同 ID、不同内容：拒绝覆盖；
- ES mapping 不兼容或主分片为 red：在向量化前失败；
- 相同 `document_id` 重复提交：PDF 幂等复用，ES 完整覆盖，不产生第二条文档；
- ES 响应不确定：以 `ingestion_attempt_id` 回读判定实际提交结果。

> **成功判定：** ES 写入响应不是唯一证据；只有按 `_id` 回读并核对本次 `ingestion_attempt_id` 与 PDF 存储键后，才能确认提交成功。

---

## 当前边界

本模块已经实现终审确认后的 PDF 与多向量入库，但尚未实现 FastAPI 路由、任务队列、业务
合同视觉精判、`contract_id` 版本关系、旧版本替换和孤儿 PDF 垃圾回收。上述能力不得与当前
文件级 `document_id` 幂等语义混为一谈。

> **当前限制：** 文件级幂等已实现，但不等于业务合同判重、`contract_id` 关系或旧版本安全替换已经可用。

---

## 验证状态

2026-08-03 使用最新 5 份成功 Mock 对正式子图进行四向量真实验证。运行前先删除并重建
`contracts-ingestion-experiment-v1`；最终 5/5 成功，ES 恰有 5 个唯一 `_id`，合同名称、产品
名称、摘要和 PDF 视觉四类向量均强制取回并确认 2048 维，对方公司向量不存在；稀疏投影
无空值，5 个 `data/contracts/<document_id>.pdf` 均通过 SHA-256 校验。证据见
`experiments/outputs/contract_ingestion_persistence/20260803T020132810771Z/`；当前全量单元测试为
183 passed。
