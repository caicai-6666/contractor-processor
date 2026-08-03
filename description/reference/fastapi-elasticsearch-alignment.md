# FastAPI 与 Elasticsearch 协议对齐

> **定位：** 在 HTTP 路由尚未实现时，先固定终审对象、检索投影与写入边界，避免服务化阶段改写核心工作流。

---

## 模块用途

本模块在尚未创建 HTTP 路由的阶段，先固定前后端终审对象和 Elasticsearch 文档结构，
避免服务化时再次改写核心工作流。

---

## 主要职责

- 定义异步受理、完整候选展示和专家确认 DTO；
- 让 FastAPI 依赖取得正式 `ProcessContract` 与独立 `IngestReviewedContract` 用例；
- 生成终审稀疏投影和四向量 Elasticsearch strict mapping；
- 以 Repository 隔离 Elasticsearch Client，并限制正式写入发生在专家确认之后。

> **写入门禁：** 模型提取结果只供展示和审核；只有专家确认后的完整包络，才能进入独立入库用例。

---

## 对外接口

- `ContractProcessAccepted`：未来上传接口返回 `job_id` 与任务状态；
- `ContractProcessResponse`：完整展示 Core、Attribute、Clause、Abstract 和处理元数据；
- `ContractReviewConfirmation`：专家修正后提交 `document_id + review + result` 完整包络，
  `review` 只包含审核员、带时区审核时间和审核意见，并自动校验内外层 `document_id`；
- `await ElasticsearchMappingFactory.build(vector_dimensions=...)`：异步生成索引 mapping；
- `await IngestReviewedContract.execute(confirmation, source_pdf)`：通过独立四节点 LangGraph 保存
  PDF，并按 `document_id` 作为 ES `_id` 覆盖写入专家确认结果和四类向量。

> **接口状态：** DTO 与依赖注入边界已固定；HTTP routes 和后台任务仍未实现，`job_id` 仅为未来异步接口的返回约定。

---

## 关键实现与设计决策

- 根 mapping 使用 `dynamic: strict`，避免前后端未约定字段静默进入索引。
- FastAPI 合同 DTO 是完整终审对象；Elasticsearch 文档是其面向检索的稀疏投影。字段发现
  结果使用独立 DTO，不进入正式合同索引。
- `ContractProcessResponse` 与单文件 CLI 返回的 production 结果结构完全一致；前端回传时
  不在 `result` 内增加审核字段，而是在外层 `review` 中携带 `reviewer`、`reviewed_at` 和
  `comment`。三个字段均为必填，审核时间必须包含时区。
- Core 与 Attribute 的动态规范值分别使用 `core_values`、`attribute_values` flattened 投影，
  清洗后的完整终审包络保存在 `reviewed_result` 且关闭 mapping 解析，避免 mapping explosion。
- Clause 使用 `nested` 保持条款内部字段关系；摘要栏目用 `flattened`，摘要正文用 `text`。
- mapping 已固定四个向量字段：合同名称、产品、摘要与 PDF 视觉向量；对方公司名称只作为
  SmartCN 文本元数据保存，不生成 dense vector。向量维度必须
  同时匹配 Embedding 配置和已有索引，否则启动失败。视觉判重与安全替换仍遵循
  [合同终审入库与多模态判重设计](../architecture/contract-ingestion-deduplication.md)。

> **对象分层：** API 使用可审核的完整终审对象；Elasticsearch 只保存为检索而裁剪的稀疏投影。两者不得混用。

---

## 3. Elasticsearch 检索投影与空字段省略

### 3.1 合法性

Elasticsearch mapping 描述“允许出现的字段结构”，不是 JSON Schema 的必填字段清单。
根 mapping 与 Core object 虽然使用 `dynamic: strict`，但这只禁止未定义字段进入文档；已定义
的 Core、Attribute、Clause 或对象子字段在某份合同中不存在时可以合法省略。省略字段不会
由 Elasticsearch 自动补写为 `null`，也不会导致写入失败。

### 3.2 双对象协议

| 对象 | 用途 | 字段保留规则 |
| --- | --- | --- |
| 完整终审对象 | 专家审阅、证据追溯、API 返回 | 保留每个配置字段的 `found`、`not_found`、`ambiguous`、`conflicting`、`not_applicable` 等终态，以及原文和理由 |
| Elasticsearch 检索投影 | 筛选、聚合、召回后的元数据读取 | 只保留 `status: found` 且规范值非空的字段；没有保留子字段的 object 和空数组也一并省略 |

Core 标量字段只有在 `status=found` 且 `value` 非空时进入 `core`。Core object 递归裁剪直属
子字段；裁剪后没有任何可检索子字段时，整个对象字段不写入。Attribute 的 `not_found`、
`not_applicable`、`ambiguous`、`conflicting` 记录不进入 `attribute` nested 数组；只有具有
有效值的 `found` 项进入。Clause 和 Abstract 的保留策略由其自身业务完整性决定，不以
“字段值为空”为理由删除合法条款或摘要正文。

`null` 通常不建立可搜索的索引项，但仍会留在 `_source`。因此若目标包含节省存储和避免
空元数据噪声，Repository 必须在调用 `client.index()` 前主动删除字段，不能只依赖
Elasticsearch 对 `null` 的默认索引行为。

省略字段后，可用 `exists` 查询判断一份合同是否实际拥有某项元数据。每次确认写入使用
`document_id` 作为完整覆盖的 `_id`；新的稀疏投影不会保留旧版本中已被专家删除的字段。

### 3.3 实现边界

裁剪已经实现为从 `ContractReviewConfirmation` 到 `ContractSearchProjection` 的纯应用服务，
只由独立入库图的 Prepare 节点调用。它不改变生产提取结果、专家审核 DTO 或 Mock 原文件；
Repository 只接收已经完成清洗、PDF 关联和向量化的 ES 文档。

### 3.4 已确认决策（2026-08-02）

已确认采用“**专家终审后裁剪，Elasticsearch 稀疏落盘**”策略：

1. 模型提取和专家审核阶段不得删除 `not_found`、`not_applicable`、`ambiguous`、
   `conflicting` 等字段状态，完整对象是审核与审计依据；
2. 专家完成修正并确认后，入库 Prepare 节点从该最终对象生成独立稀疏检索投影；
3. 该投影删除没有有效业务值的 Core、Attribute、对象子字段、空对象和空数组，只保留
   `found` 且规范值非空的元数据；
4. 裁剪只影响 Elasticsearch 检索文档，不改变专家确认的完整业务结果，也不得前移到
   Core、Attribute、Clause 或 Abstract 的提取阶段。

> **决策结论：** 保留完整结果以支持审核与审计；仅在专家确认后生成检索投影，兼顾证据可追溯性与索引洁净度。

---

## 4. 依赖与配置

`configs/settings.yaml` 的 `elasticsearch` 节包含 HTTPS hosts、凭据环境变量名、CA 证书路径、
正式合同索引、入库实验索引和 `vector_dimensions`。当前
Qwen3-VL-Embedding-2B 使用 2048 维，必须与 `models.embedding.dimensions` 一致。密码只写入
本地 `.env`，不得写入 YAML 或提交 Git。若系统 CA 路径对应用用户不可读，应将公开的
`http_ca.crt` 复制到被 Git 忽略的 `data/certs/http_ca.crt`，配置使用该运行时副本；不要复制
私钥。

运行依赖固定为 Elasticsearch 9.x 官方 Python Client。正式合同由 Repository 写入
`index_name`。字段发现属于批处理：只有本批次新候选的多视角字段向量进入 LlamaIndex
`SimpleVectorStore` 内存索引；固定 Discovery Core/Attribute 不进入候选池。批次结束即释放，
不写入 ES，也不配置专用持久化索引。客户端
创建时必须从环境变量读取凭据，并将 `ca_certs`、`verify_certs` 原样传给官方 Client。

本地中文合同检索依赖官方 `analysis-smartcn` 插件（当前 Elasticsearch 9.4.4）。需要中文
分词的 `text` 字段应在索引创建时使用 `smartcn` analyzer；插件安装、重启、验证及已有索引
重建约束见[Elasticsearch 中文分词配置](../operations/elasticsearch-chinese-analysis.md)。

截至当前版本，官方 `llama-index-vector-stores-elasticsearch 0.6.x` 的依赖上限为
Elasticsearch Python Client `<9`，不能与本项目的 Elasticsearch 9.4.x 客户端共装。因此
当前项目只保留 `llama-index-core`。合同正式索引由项目自定义的 ES 9 LlamaIndex
`VectorStore` 适配器负责；字段发现则直接使用 `llama-index-core` 内置
`SimpleVectorStore`，两者不共享索引。不得使用 `--no-deps` 强行安装不兼容插件。

当前没有 routes，也没有后台任务系统。`ingest_reviewed_contract_dependency()` 已能取得正式
入库用例；未来 HTTP 层只需接收终审 JSON 与 PDF 并调用该用例。部署为同步等待还是返回
`job_id` 后交给 Worker，属于接口运行策略，不得改变四节点核心逻辑。

> **配置边界：** 凭据只经环境变量与本地 `.env` 提供；YAML 只保存环境变量名、公开 CA 路径及索引、维度等非敏感配置。
