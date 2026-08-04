# 项目文档导航

> 本页是项目文档的统一入口。请先按当前任务选择阅读路径，再进入具体主题；不需要从头通读全部文档。

---

## 从哪里开始

| 当前目标 | 建议先读 | 然后按需阅读 |
| --- | --- | --- |
| 了解项目目标、术语与全局约束 | [项目说明](PROJECT.md) | [项目结构](architecture/project-structure.md) |
| 修改合同处理主流程 | [合同信息化处理工作流](architecture/contract-information-workflow.md) | [合同处理工作流模块](capabilities/contract-processing-workflow.md)、[抽取服务](capabilities/extraction-services.md) |
| 修改 Core、Attribute、Clause 或摘要抽取 | [属性双运行模式设计](architecture/attribute-operating-modes.md) | 对应的字段 / 摘要目录文档与抽取质量规范 |
| 修改模型提示词 | [提示词工程规范](architecture/prompt-engineering.md) | [MLLM Prompt 共享前缀设计](architecture/mllm-prompt-prefix.md) 与对应任务文档 |
| 开发专家终审后的合同入库 | [专家终审合同入库模块](capabilities/contract-ingestion.md) | [合同终审入库与多模态判重设计](architecture/contract-ingestion-deduplication.md) |
| 本地运行、配置或排障 | [初始化与本地配置](operations/initialization.md) | [CLI 与测试入口](operations/cli-test-entrypoints.md)、[异步生产运行时](operations/async-production-runtime.md) |
| 运行或分析实验 | [实验总览](experiments/readme.md) | 对应实验说明及运行目录中的 `analysis.md` |

---

## 文档分层

文档按阅读目的划分。当前目录仍处于渐进整理阶段；下表描述各类文档的职责与后续归位方向，而不是要求一次性迁移全部文件。

| 分类 | 回答的问题 | 当前主要位置 | 典型内容 |
| --- | --- | --- | --- |
| 项目总览 | 项目为何存在、核心概念是什么、有哪些全局规则？ | 根目录 | `PROJECT.md`、本导航页 |
| 架构 | 为什么这样设计、跨模块如何协作？ | `architecture/` | 运行模式、处理流程、文档身份、提示词工程、入库与去重 |
| 业务能力 | 一个能力负责什么、如何使用、依赖哪些边界？ | `capabilities/` | 抽取、字段发现、合同摘要、合同入库 |
| 参考契约 | 字段、状态、DTO、索引或机器规则究竟是什么？ | `reference/`、`fields/`、`data/definitions/` | 字段定义契约、Core / Attribute / Clause 定义、API 与 Elasticsearch 对齐 |
| 运行维护 | 如何配置、执行、测试与排障？ | `operations/` | 初始化、CLI、异步运行时、中文分词 |
| 实验验证 | 如何复现、验证和分析某项方案？ | `description/experiments/` 与项目根目录 `experiments/` | 实验说明、入口与输出 |

> **阅读提示：** 架构文档解释长期设计决策；能力文档描述具体模块职责；参考契约给出精确规则；运行与实验文档面向执行。相同内容只应有一个主文档，其他文档以链接引用。

---

## 架构与全局规范

- [项目说明](PROJECT.md)：项目目标、范围、核心术语、全局边界与质量原则。
- [文档撰写风格手册](DOCUMENTATION_STYLE_GUIDE.md)：文档命名、分层、排版、链接与审查要求。
- [项目结构](architecture/project-structure.md)：分层边界、源码目录职责与本地运行方式。
- [属性双运行模式设计](architecture/attribute-operating-modes.md)：`discovery` 与 `production` 的边界。
- [合同信息化处理工作流](architecture/contract-information-workflow.md)：正式合同处理的端到端流程。
- [文档身份协议](architecture/document-identity.md)：以 PDF SHA-256 为中心的身份规则。
- [提示词工程规范](architecture/prompt-engineering.md)：提示词编排、`reason` 收束与结构化信息展示规则。
- [MLLM Prompt 共享前缀设计](architecture/mllm-prompt-prefix.md)：多模态输入分层与前缀缓存边界。
- [字段发现两阶段工作流](architecture/field-discovery-workflow.md)：候选生成、归并、收敛与统计。
- [合同终审入库与多模态判重设计](architecture/contract-ingestion-deduplication.md)：终审包络、向量职责与判重规划。

---

## 业务能力与参考契约

### 字段与内容抽取

- [字段定义契约](reference/field-definition-contract.md)：Core 与正式 Attribute 的通用结构、约束与候选定义编译规则。
- [Core 字段目录](fields/core/core.md)：Core 业务语义、结果包络与字段级提取规则。
- [Core 字段目录版本迁移记录](fields/core/core-version-history.md)：历史字段结构变更与重新提取要求。
- [Attribute 字段目录](fields/attribute/attribute.md)：固定 Attribute 与候选治理规则。
- [Clause 说明](fields/clause/clause.md)：条款的结构、边界和输出约定。
- [Clause 条款提取版本记录](fields/clause/clause-version-history.md)：条款提取的历史设计演进。
- [Contract Summary 说明](contract-summary/contract-summary.md)：合同级固定摘要与向量检索协议。
- [Attribute 提取质量规范](capabilities/attribute-extraction-quality.md)：语义门禁、局部重试与扩展准则。
- [抽取服务](capabilities/extraction-services.md)：正式 Core、Attribute、Clause 与摘要服务的实现边界。

### 工作流、检索与入库

- [合同处理工作流模块](capabilities/contract-processing-workflow.md)：统一异步子图、IDE 入口与候选协议。
- [字段发现批次内存向量索引](capabilities/field-discovery-memory-vector-index.md)：候选摘要索引的生命周期。
- [字段发现分组收敛实验模块](experiments/field-discovery-group-consolidation.md)：共享归并实现与复现实验入口。
- [专家终审合同入库模块](capabilities/contract-ingestion.md)：独立入库子图、PDF 存储与幂等写入。
- [FastAPI 与 Elasticsearch 协议对齐](reference/fastapi-elasticsearch-alignment.md)：DTO、稀疏投影与索引 mapping。

---

## 运行维护与实验

### 运行维护

- [初始化与本地配置](operations/initialization.md)
- [CLI 与测试入口](operations/cli-test-entrypoints.md)
- [异步生产运行时](operations/async-production-runtime.md)
- [Elasticsearch 中文分词配置](operations/elasticsearch-chinese-analysis.md)

### 实验验证

- [实验总览](experiments/readme.md)
- [Core 抽取实验](experiments/core-extraction.md)
- [Clause 抽取实验](experiments/clause-extraction.md)
- [合同终审入库实验](experiments/contract-ingestion.md)
- [合同视觉检索实验](experiments/contract-visual-retrieval.md)
- [合同视觉鲁棒性实验](experiments/contract-visual-robustness.md)
- [合同四向量内存召回实验](experiments/contract-vector-retrieval.md)

> **实验分析：** 当任务涉及某次实验的结果分析时，除对话结论外，还必须在该运行输出目录的 `analysis.md` 中追加记录。具体格式与证据要求以项目根目录的 `AGENTS.md` 为准。

---

## 渐进整理约定

后续调整文档时遵循以下规则，避免目录迁移造成链接和阅读路径失效：

1. 新建文档先判断其阅读目的，再选择对应分类；不要因实现文件所在目录而机械归类。

2. 迁移既有文档时，以一个完整主题为单位完成：移动文件、更新所有相对链接、更新本导航页，并检查文档内锚点。

3. 不重复复制同一规则。主文档保留完整说明，引用处只保留必要上下文和相对链接。

4. 目录调整后，更新所有相对链接、本导航页和需要引用该主题的上层文档；本页是稳定的阅读入口。
