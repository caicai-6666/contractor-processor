# 项目结构与演进架构

> 状态：正式代码结构。
> 当前运行形态：合同提取主图与专家终审入库图相互独立。
> 服务边界：已建立 FastAPI DTO/依赖目录和 Elasticsearch 适配器，当前不创建路由。

---

## 快速导航

- [依赖方向](#2-依赖方向)：各层之间允许的调用方向。
- [目标目录](#3-目标目录)：源码目录与职责。
- [核心端口](#5-核心端口)：领域与基础设施的隔离点。
- [正式入口](#6-当前正式用例与入口)：可运行用例与当前状态。

---

## 1. 架构决策

项目采用 FastAPI 友好的分层结构，但当前阶段不创建业务路由。`interfaces/api/schemas/`
已经冻结受理、终审展示和确认 DTO，`interfaces/api/dependencies.py` 可取得正式
`ProcessContract` 和 `IngestReviewedContract`。未来路由只能调用用例，不能复制工作流逻辑。

项目目标包含 `discovery` 与 `production` 两种显式运行模式。两者的 Attribute 服务、状态和
结果必须隔离；完整决策见 [Attribute 双运行模式设计](attribute-operating-modes.md)。两套
用例、状态、结果 DTO、LangGraph 拓扑和固定 Attribute 提取已经实现；具体字段发现算法
仍未实现，生产 Attribute 在非空目录时使用逐字段固定提取服务。

> **架构底线：** API、CLI 与图编排只能调用应用用例；领域规则、字段治理与提示词业务语义不能下沉到路由、客户端或框架对象中。

---

## 2. 依赖方向

```text
interfaces (CLI / future API)
            ↓
application (用例与工作流编排)
            ↓
domain (业务模型与纯业务规则)
            ↑
infrastructure (PDF、LangGraph、Qwen、LlamaIndex、Elasticsearch 等实现)
```

- `domain` 不得依赖 FastAPI、vLLM、Elasticsearch、文件路径或具体向量数据库。
- `application` 通过自身的端口（抽象接口）使用外部能力，负责一个处理步骤的顺序与事务边界。
- `infrastructure` 实现端口，可随部署环境替换。
- `interfaces` 负责参数解析、认证或 HTTP 协议转换，不包含抽取、归并或频次统计规则。

> **依赖规则：** 外部框架对象必须在基础设施层转换为领域模型或应用 DTO；不得穿透到 `domain` 或 `application` 的公共接口。

---

## 3. 目标目录

当前 Python 代码采用如下 `src` 布局：

```text
src/contract_processor/
├── domain/
│   ├── models.py           # Contract、FieldDefinition、Evidence、Attribute 等
│   ├── enums.py            # FieldKind、ReviewStatus 等
│   └── policies.py         # 归并、统计、升级等纯业务规则
├── application/
│   ├── use_cases/          # 面向业务动作的用例
│   ├── workflows/          # 框架无关的工作流节点与状态转换
│   ├── ports/              # 外部能力抽象接口
│   ├── prompts/            # 提示词模板与字段定义注入策略
│   └── schemas/            # 从字段目录动态生成结构化抽取 Schema
├── infrastructure/
│   ├── pdf/                # PDF 页面化与预处理
│   ├── extraction/         # Core/空 Core、Clause、Abstract 与当前 Attribute 空策略
│   ├── llm/                # OpenAI SDK 封装的本地 vLLM/Qwen 客户端
│   ├── rag/                # LlamaIndex 字段/合同检索适配器
│   ├── orchestration/      # LangGraph 图构建、路由与检查点适配器
│   ├── persistence/        # PDF 文件、Elasticsearch 和字段目录读写
│   └── observability/      # 运行日志、模型与提示词版本记录
├── interfaces/
│   ├── cli/                # 单文件、批量、新功能验证与验收入口
│   └── api/                # FastAPI DTO 与依赖；当前不创建 routes
├── bootstrap/              # 配置加载与依赖组装
└── settings.py             # Pydantic 运行配置模型

configs/                    # 非敏感运行配置
data/                       # 版本化机器规范、敏感输入和确认后业务数据
tests/
├── unit/                   # domain 与 application 的快速测试
├── integration/            # PDF、模型、向量库与持久化集成测试
└── fixtures/               # 可公开、可复现的小样本
description/                # 项目、字段、合同摘要、架构与模块说明
data/definitions/           # 生产机器规范及与其隔离的 Discovery Core/Attribute 初始目录
```

`interfaces/cli/run_single_file.py` 和 `run_batch.py` 通过 `--mode` 选择运行用例，省略时读取
`settings.runtime.mode`。未来生产 API 固定使用 `build_process_contract`，不会因配置默认值
意外进入发现模式。需要保存结果或报告的新功能实验必须放在 `experiments/`；正式 CLI 只
展示结果。

> **运行入口：** 模式选择必须显式并进入对应应用用例；需要保存调试产物的功能必须经由实验入口，不能给正式 CLI 增加本地业务落盘副作用。

---

## 4. 技术栈边界

| 技术 | 放置位置 | 负责内容 | 不得负责 |
| --- | --- | --- | --- |
| LangGraph | `infrastructure/orchestration/` | 组装图、节点路由、失败重试、检查点 | Core/Attribute 字段规则与提示词业务语义 |
| LlamaIndex | `infrastructure/rag/`、`infrastructure/persistence/` | Discovery 新候选批次内存召回与 ES9 合同多向量 VectorStore 适配 | 候选字段身份、分组或升级的最终判断 |
| OpenAI Python SDK | `infrastructure/llm/` | 对接本地 vLLM 的 OpenAI 兼容接口、结构化输出和重试 | 领域数据模型与工作流编排 |

LangGraph 的节点应调用 `application` 中的节点处理函数；图本身不应成为业务规则的唯一载体。LlamaIndex 的 `Document`、`Node`、检索响应以及 OpenAI SDK 的响应对象，都必须在基础设施层转换为项目自己的领域模型或应用 DTO，禁止向上泄漏。

Core、Clause 和 Contract Summary 读取同一 PDF 时，必须遵循 [MLLM Prompt 共享前缀设计](mllm-prompt-prefix.md)：统一渲染一次页面，复用完全一致的 system、公共规则、页面图像和页面上下文；Core Map 与 Clause Map 可以进一步共享地图规则，但保持独立 Schema 和结果。Prefix cache 只用于性能优化，不能成为正确性依赖。

> **技术栈边界：** LangGraph 负责拓扑，LlamaIndex 负责召回，模型客户端负责调用；它们都不能替代字段身份、分组、升级或业务规则的确定性决策。

---

## 5. 核心端口

应用层通过以下端口隔离外部依赖：

| 端口 | 职责 | 首期实现 |
| --- | --- | --- |
| `PdfRenderer` | 将 PDF 转换为可供视觉模型理解的页面与证据坐标 | 本地 PDF 渲染器 |
| `VisionModelClient` | 调用 Qwen2.5-VL 并返回结构化结果 | OpenAI SDK 封装的本地 vLLM 客户端 |
| `FieldCatalog` | 读取、校验并写入 Core/Attribute 字段定义 | `data/definitions` 下的 YAML |
| `FieldDiscoveryService` | 消费原始页面、固定 Core/Attribute 结果与定义并返回新候选 | 端口已建立，协议尚需扩展后接入 |
| `FieldSimilaritySearcher` | 只在本批次新字段身份之间执行 Top 5 向量召回 | LlamaIndex `SimpleVectorStore` 批次内存索引 |
| `SourceDocumentStore` | 按 `document_id` 保存并读取专家已确认合同的原始 PDF | 本地文件系统；后续可替换对象存储 |
| `ContractIndexRepository` | 保存专家确认后的元数据、检索向量和合同视觉判重向量 | Elasticsearch |
| `ReviewExporter` | 生成按频次排序的专家审核产物 | Markdown 与 CSV 文件 |

端口以协议或抽象基类定义；应用层接收端口实例，而不是在工作流中直接创建客户端。

> **端口原则：** 应用层依赖抽象能力而非部署实现；替换 PDF、模型、检索或存储基础设施不得改变领域和用例语义。

---

## 6. 当前正式用例与入口

当前已经实现 `ProcessContract`；`DiscoverContractFields` 的应用与拓扑边界也已建立，但正式默认发现服务尚未迁移。其余批次、确认和审核用例沿用下列边界逐步接入：

| 用例 | 状态 | 输入 | 输出 |
| --- | --- | --- |
| `ProcessBatch` | 规划 | 合同目录、字段库版本、运行配置 | 批次编号、逐份合同自动化候选、运行指标 |
| `ProcessContract` | 已实现 | 单份 PDF、固定字段目录版本 | Core、固定 Attribute、Clause 和 Abstract 自动化最终候选 |
| `DiscoverContractFields` | 部分实现 | 单份 PDF、Core/Attribute 目录版本 | 独立发现结果；禁用 Clause/Abstract；具体发现服务待注入 |
| `DiscoverFieldsFromBatch` | 规划 | 固定合同集、独立 Discovery 目录及模型版本 | 新候选身份、语义分组、全量回扫统计与审核包 |
| `IngestReviewedContract` | 已实现 | 专家直接修正并确认的最终对象、原始 PDF | PDF 落盘结果和 Elasticsearch 写入结果 |
| `ConsolidateAttributes` | 规划 | 本轮候选 Attribute | 归并结果、频次统计、字段库变更建议 |
| `ExportAttributeReview` | 规划 | 批次编号 | 按不同合同数降序的 Markdown/CSV 审核文件 |
| `PromoteAttributeToCore` | 规划 | 经专家确认的字段 ID | Core 字段库更新与迁移审计记录 |

当前字段目录检查方式：

### 字段目录检查

```text
运行 python -m contract_processor.interfaces.cli.inspect_fields
```

该命令应显示 Core 12 个、Attribute 10 个。production 单文件入口会执行固定 Attribute
提取；真实 vLLM 回归应同时检查 Attribute 的字段覆盖和最终阶段校验。

---

## 7. 本地数据与可追溯性

`data/` 承载版本化机器规范、敏感输入和专家确认后的业务文件。正式自动候选不在本地落盘：

```text
data/
├── definitions/             # 生产机器规范与独立 Discovery 初始目录；提交版本库
├── input/<batch_id>/        # 原始合同 PDF；敏感输入，不提交版本库
├── contracts/<document_id>.pdf # 专家确认后落盘的原始 PDF
└── logs/                    # 结构化日志
```

合同文档唯一标识使用原始 PDF 文件完整字节流的 SHA-256 小写十六进制值。该值由程序在渲染和模型调用前计算，模型不生成；完整协议见 [合同文档身份协议](document-identity.md)。`contract_number` 是可空且可重复的业务字段，缺失或冲突不阻断入库。每项提取结果必须关联 `document_id`、页码/坐标、原始证据、模型版本、提示词版本和字段库版本。

PDF 页面默认在工作线程中逐页渲染后直接传给 MLLM，不持久化页面图片。排障、失败复现或
人工核对所需文件只允许由 `experiments/` 保存，不得作为正式处理的前置依赖。

合同摘要的正式实现直接读取完整原 PDF，一次提取固定六栏目，再对未通过业务校验的栏目
执行局部重试，最终由程序固定渲染。`experiments/contract_summary_generation/run.py` 只是该
实现的独立研发入口。Core、Attribute 和 Clause 不作为摘要模型输入，仍作为合同向量
记录的元数据，并可用于离线质量对照。

正式业务数据只在专家最终校验并确认后写入：原始 PDF 由 `SourceDocumentStore` 落盘，
Core、Attribute、Clause、Abstract、字段检索向量和合同视觉判重向量由
`ContractIndexRepository` 写入
Elasticsearch。自动化候选、冲突比较和审核修订历史不作为正式业务版本保存。实验运行器
可继续在 `experiments/outputs/` 保存研发调试产物，两者不能混用。完整流程见
[合同信息化处理工作流](contract-information-workflow.md)。尚未实现的多模态判重与版本替换
指导见[合同终审入库与多模态判重设计](contract-ingestion-deduplication.md)。

> **数据边界：** 自动化候选、页面和调试材料属于内存或实验产物；只有专家确认后的 PDF、元数据与目标向量才能进入正式存储。

---

## 8. FastAPI 与 Elasticsearch 对齐

当前接口层结构为：

```text
interfaces/api/
├── schemas/contracts.py     # 异步受理、终审展示和完整确认 DTO
└── dependencies.py          # 与 IDE 共用的应用用例依赖
```

终审前的候选只在内存或任务系统中传递；专家完整确认后，未来路由调用独立
`IngestReviewedContract`，由其四节点图保存 PDF，并以 `document_id` 为 `_id` 写入正式索引。
耗时较长时可由接口返回 `job_id` 后交给 Worker，但路由不得直接调用 ES 或 Embedding 客户端。

> **未来 API 边界：** 路由只负责协议转换与任务受理；确认后的入库必须通过独立 `IngestReviewedContract` 用例执行。

---

## 9. 当前边界

- 不创建 FastAPI 路由、任务队列、容器编排或多租户代码。
- 不让领域模型返回 HTTP 状态码、框架异常或 ORM 对象。
- 不在模型客户端中写字段归并规则，也不在路由或 CLI 中拼装提示词。
- 不让 LangGraph、LlamaIndex 或 OpenAI SDK 的对象穿透到 `domain` 或 `application` 的公共接口中。
- Core、Attribute、Clause 和摘要机器规范维护在 `data/definitions/`；运行时由配置路径读取，
  禁止散落硬编码在 Python 模块中。`description/` 只保存解释性文档。
- `DiscoverContractFields` 的端口、DTO 与拓扑已实现，正式默认字段发现算法尚未迁移；CLI 已
  支持模式切换，选择 discovery 时会 fail closed。第一大步批处理实验已经用一条流水线覆盖固定
  Core/Attribute、新字段发现与规则门禁、LlamaIndex 批次内 Top 5、多路排名融合、LLM 三分类、
  确定性分组和组级字段收敛；`DiscoverFieldsFromBatch`、第二阶段统计、候选治理和 Attribute
  Profile 的选择与版本治理仍是后续开发边界。

字段发现目标算法使用独立 Discovery Core/Attribute 作为固定约束，只把本批次新字段身份
加入候选向量池；第一大步五节点统一流水线和第二阶段全量统计协议见
[字段发现两阶段工作流](field-discovery-workflow.md)。

> **当前限制：** 未实现的字段发现、业务判重、版本替换和 HTTP 服务必须显式失败或保持未注册状态；不得通过放宽字段契约或越过用例层来伪造支持。
