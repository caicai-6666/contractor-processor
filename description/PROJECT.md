# 合同元数据发现 Agent 工作流：项目说明

> **项目定位：** 本项目面向 PDF 合同，提供字段发现与正式提取两种显式运行模式；自动化结果均需具备证据、版本与专家审核边界。

本文件只说明项目目标、全局边界和阅读入口。字段契约、工作流细节、模块实现与运行方式请阅读对应专题文档；完整导航见[项目文档导航](README.md)。

---

## 1. 项目目标

本项目构建面向 PDF 合同的 Agent 工作流，通过两种显式运行模式分别完成元数据建模和正式合同提取。

- **字段发现模式（`discovery`）**：以独立的 Discovery Core / Attribute 作为固定覆盖约束，开放发现尚未建模的字段。两个目录均允许为空，从而支持项目冷启动；该模式禁用 Clause 和 Abstract。

- **正式生产模式（`production`）**：Core 与 Attribute 都是专家确认、带版本的固定字段；Core 至少包含一个字段。系统严格按定义提取 Core、Attribute、Clause 与 Abstract，禁止临时创造字段。空 Core 属于启动配置错误；Attribute 可为空，空目录时不注册提取节点并稳定返回 `attribute: []`。

> **模式边界：** `discovery` 用于发现与治理候选字段；`production` 只按经过确认的固定字段提取。候选未经专家审核，不能写入正式字段目录或正式存储。

发现模式产生的 Attribute Candidate 会被归并、计数并交由专家审核；专家可以决定将成熟候选纳入 Core、纳入正式 Attribute、与已有字段合并或拒绝。Core 字段将作为后续 RAG 检索的首要过滤条件，因此其正确性、规范性与可追溯性高于字段覆盖数量。

---

## 2. 范围与非目标

### 范围

- 输入：固定批次的 PDF 合同，包括文本型、扫描型及带签章的合同。
- 内容理解：通过本地 vLLM 使用 `configs/settings.yaml` 配置的多模态模型，对合同页图像进行理解和结构化提取。
- 发现模式输出：带原文证据、统计和治理信息的字段候选及字段库变更建议。
- 生产模式输出：合同级结构化 Core、固定 Attribute、完整 Clause 条款实例和固定格式 Abstract；自动候选经专家确认后才能进入正式存储。
- 人机协作：自动化结果汇总后由专家对照原始 PDF 进行最终校验和直接修正；专家确认后形成正式落盘结果。

### 非目标（首期）

- 不在首期自动将 Attribute 提升为 Core；升级决定必须由人类专家作出。
- 不以替代合同审阅、法律意见或自动判断合同有效性为目标。
- 不追求为每一个低频、无检索价值的条款细节建立字段。

---

## 3. 核心术语

| 术语 | 定义 |
| --- | --- |
| Core | 跨多数合同稳定出现、业务重要且适合作为 RAG 前置过滤条件的字段。 |
| Attribute Candidate | 发现模式提出、尚未经专家确认的潜在字段定义。 |
| Attribute Definition | 专家确认后进入正式目录的固定扩展字段，通常适用于特定合同类型或业务领域。 |
| Attribute Extraction | 两种模式共享的固定字段提取能力；production 读取正式目录，discovery 读取独立初始目录。 |
| Clause | 按合同原始顺序保留具有自有原文标题或可继承明确父标题的规范性条款，不混入主体、金额表或签署资料，也不作为 Core 筛选字段。 |
| 字段库 | 保存正式 Core 和 Attribute 定义、别名、提取规则及示例的版本化知识库。 |
| 候选治理库 | 保存 Attribute Candidate 的来源、统计、相似字段、审核状态和归并历史。 |
| 字段归并 | 将新发现字段与本批次新候选池进行 Top 5 召回和大模型三分类，决定复用身份、关联分组或新建分组。 |
| 一轮处理 | 对一个固定合同批次进行完整遍历、字段归并与统计导出的迭代。 |

---

## 4. 权威规范与字段目录

> **机器规范优先：** 字段目录、输出 Schema 与本地校验共同构成可执行契约；自然语言提示词或说明文档不得替代机器约束。

| 主题 | 机器规范 | 说明与设计入口 |
| --- | --- | --- |
| Core | [core.yaml](../data/definitions/core.yaml) | [Core 字段目录](fields/core/core.md) |
| Attribute | [attribute.yaml](../data/definitions/attribute.yaml) | [Attribute 字段目录](fields/attribute/attribute.md) |
| Clause | [clause.yaml](../data/definitions/clause.yaml) | [Clause 说明](fields/clause/clause.md) |
| Contract Summary | [contract_summary.yaml](../data/definitions/contract_summary.yaml) | [Contract Summary 说明](contract-summary/contract-summary.md) |
| 字段定义通用契约 | 对应的 Core / Attribute YAML | [字段定义契约](reference/field-definition-contract.md) |

字段定义的递归类型、空值语义、`extraction_rule`、候选定义编译与规则 / 证据边界，以[字段定义契约](reference/field-definition-contract.md)为唯一解释入口。字段发现的向量召回、Top 5 判别、关系图分组、组级收敛与全合同集回扫，以[字段发现两阶段工作流](architecture/field-discovery-workflow.md)为准。

合同级产物以原始 PDF 文件字节的 SHA-256 作为 `document_id`；合同编号是可空、可重复的业务字段，缺失、冲突或重复不阻断摘要和索引。完整身份协议见[文档身份协议](architecture/document-identity.md)。

> **稀疏投影原则：** 专家终审对象保留完整审计信息；Elasticsearch 只投影 `found` 且具有非空规范值的字段。未出现的字段应省略，不能用 `null` 或空对象占位。

---

## 5. 双运行模式总览

```text
合同文件集 / 单份 PDF
  → PDF Prepare
  ├─ 按运行模式装配字段目录快照
  │    └─ 共享固定字段链：Core / Empty Core → Attribute / Empty Attribute
  │         ├─ Discovery → Field Discovery → 候选池分组归并
  │         │              → 全合同集命中统计 → 专家审核字段
  │         │              → 新版 Discovery / Production 字段目录
  │         └─ Production ────────────────────────────────┐
  │                                                        │
  ├─ Production only：Clause ──────────────────────────────┼→ 结果汇总 → 专家审核合同 → 正式存储
  └─ Production only：Abstract ────────────────────────────┘
```

两种模式共享 Core → Attribute 的字段目录解析、动态 Schema、逐字段提取、结果校验、失败隔离和 Core 上下文注入能力，但装配不同目录快照。Attribute 始终以原始 PDF 为事实来源；Clause 与 Abstract 不消费 Core 或 Attribute 结果，并与该链路并行。

完整的模式隔离、节点拓扑和副作用边界见[Attribute 双运行模式设计](architecture/attribute-operating-modes.md)；正式合同处理的输入、输出与专家终审流程见[合同信息化处理工作流](architecture/contract-information-workflow.md)。

---

## 6. 质量原则

- **Core 优先**：宁可将不确定字段保留在 Attribute 审核队列，也不要错误地污染 Core。
- **证据优先**：每个提取值和字段演进均应能回溯到合同、页码 / 位置与原始文本或图像证据。
- **规范化输出**：日期、金额、枚举等必须遵循字段的 `output` 定义；原文与规范化结果应同时保留。
- **去重统计**：同一份合同同一字段只计入一次“不同合同出现数”。
- **可迭代**：每一轮均保留输入批次、字段库版本、模型 / 提示词版本与产出，便于对比与回归验证。

---

## 7. 待确认事项

- [Core 字段目录](fields/core/core.md)中哪些字段需要保留、拆分或调整优先级。
- 多视角候选向量的排名融合参数和经过真实标注校准的噪声过滤阈值。
- 多个目标同时被判定为 `same` 时，字段池重复冲突的专家处置协议。
- Attribute 升级为 Core 的专家决策标准，例如不同合同出现数、业务重要性与定义稳定性。
- 候选批准为正式 Attribute 而非 Core 的准入标准，以及 Attribute Profile 的选择规则。
- 专家审核产物的具体格式（Markdown、CSV 或两者）及字段库的持久化格式。

---

## 8. 下一步阅读

按任务选择专题文档，不在本文件中重复维护专题规则：

- 修改提示词：阅读[提示词工程规范](architecture/prompt-engineering.md)和[MLLM Prompt 共享前缀设计](architecture/mllm-prompt-prefix.md)。
- 修改字段或字段发现：阅读[字段定义契约](reference/field-definition-contract.md)、对应字段目录与[字段发现两阶段工作流](architecture/field-discovery-workflow.md)。
- 修改处理、入库、检索或外部接口：从[项目文档导航](README.md)选择对应能力、运行或实验文档。
