# 项目结构与演进架构

> 状态：首期架构草案。  
> 当前运行形态：本地单进程 CLI，面向固定合同批次。  
> 演进目标：在不改写核心工作流的前提下，增加 HTTP 服务、异步任务和多用户能力。

## 1. 架构决策

首期不引入 FastAPI。项目的核心价值是合同理解、字段抽取、字段归并和审计，而非 HTTP 路由。应先将这些能力实现为独立的应用用例，再由 CLI 调用。

当需要持续接收新合同、对外集成或多用户审核时，FastAPI 仅作为 `interfaces/api/` 中的另一种入口：路由调用与 CLI 相同的应用用例，不能复制工作流逻辑。

## 2. 依赖方向

```text
interfaces (CLI / future API)
            ↓
application (用例与工作流编排)
            ↓
domain (业务模型与纯业务规则)
            ↑
infrastructure (PDF、LangGraph、Qwen、LlamaIndex、SQLite 等实现)
```

- `domain` 不得依赖 FastAPI、vLLM、SQLite、文件路径或具体向量数据库。
- `application` 通过自身的端口（抽象接口）使用外部能力，负责一个处理步骤的顺序与事务边界。
- `infrastructure` 实现端口，可随部署环境替换。
- `interfaces` 负责参数解析、认证或 HTTP 协议转换，不包含抽取、归并或频次统计规则。

## 3. 目标目录

待进入实现阶段后，Python 代码采用如下 `src` 布局：

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
│   ├── llm/                # OpenAI SDK 封装的本地 vLLM/Qwen 客户端
│   ├── rag/                # LlamaIndex 字段/合同检索适配器
│   ├── orchestration/      # LangGraph 图构建、路由与检查点适配器
│   ├── persistence/        # SQLite、文件和字段目录读写
│   └── observability/      # 日志、审计、模型与提示词版本记录
├── interfaces/
│   ├── cli/                # 当前唯一运行入口
│   └── api/                # 未来 FastAPI 入口；首期不创建
├── bootstrap/              # 配置加载与依赖组装
└── settings.py             # 运行配置模型

configs/                    # 非敏感运行配置
data/                       # 本地运行数据，不提交版本库
tests/
├── unit/                   # domain 与 application 的快速测试
├── integration/            # PDF、模型、向量库与持久化集成测试
└── fixtures/               # 可公开、可复现的小样本
scripts/                    # 一次性或运维辅助命令
description/                # 项目、字段、架构与模块说明
```

现阶段不必创建空的 `src/` 目录或 FastAPI 路由；开始实现首个用例时再按此结构落地。

## 4. 技术栈边界

| 技术 | 放置位置 | 负责内容 | 不得负责 |
| --- | --- | --- | --- |
| LangGraph | `infrastructure/orchestration/` | 组装图、节点路由、失败重试、检查点 | Core/Attribute 字段规则与提示词业务语义 |
| LlamaIndex | `infrastructure/rag/` | 字段摘要和合同分块的索引、召回、RAG 上下文组装 | Attribute 是否合并或升级的最终判断 |
| OpenAI Python SDK | `infrastructure/llm/` | 对接本地 vLLM 的 OpenAI 兼容接口、结构化输出和重试 | 领域数据模型与工作流编排 |

LangGraph 的节点应调用 `application` 中的节点处理函数；图本身不应成为业务规则的唯一载体。LlamaIndex 的 `Document`、`Node`、检索响应以及 OpenAI SDK 的响应对象，都必须在基础设施层转换为项目自己的领域模型或应用 DTO，禁止向上泄漏。

## 5. 核心端口

应用层通过以下端口隔离外部依赖：

| 端口 | 职责 | 首期实现 |
| --- | --- | --- |
| `PdfRenderer` | 将 PDF 转换为可供视觉模型理解的页面与证据坐标 | 本地 PDF 渲染器 |
| `VisionModelClient` | 调用 Qwen2.5-VL 并返回结构化结果 | OpenAI SDK 封装的本地 vLLM 客户端 |
| `FieldCatalog` | 读取、校验并写入 Core/Attribute 字段定义 | `description/fields` 下的 YAML |
| `FieldSimilaritySearcher` | 对字段摘要向量召回候选字段 | LlamaIndex 检索器与本地向量索引 |
| `MetadataRepository` | 保存合同、抽取值和原始证据 | SQLite |
| `RunRepository` | 保存批次、状态、模型版本与提示词版本 | SQLite |
| `ReviewExporter` | 生成按频次排序的专家审核产物 | Markdown 与 CSV 文件 |

端口以协议或抽象基类定义；应用层接收端口实例，而不是在工作流中直接创建客户端。

## 6. 首期用例与 CLI

首期只提供本地命令行入口。建议的用例边界如下：

| 用例 | 输入 | 输出 |
| --- | --- | --- |
| `ProcessBatch` | 合同目录、字段库版本、运行配置 | 批次编号、逐份合同结果、审计记录 |
| `ProcessContract` | 单份合同、批次上下文 | Core 值、候选 Attribute、证据 |
| `ConsolidateAttributes` | 本轮候选 Attribute | 归并结果、频次统计、字段库变更建议 |
| `ExportAttributeReview` | 批次编号 | 按不同合同数降序的 Markdown/CSV 审核文件 |
| `PromoteAttributeToCore` | 经专家确认的字段 ID | Core 字段库更新与迁移审计记录 |

对应 CLI 可以保持简洁：

```text
contract-processor process-batch <input-directory>
contract-processor export-attribute-review <batch-id>
contract-processor promote-attribute <field-id>
```

## 7. 本地数据与可追溯性

`data/` 是运行时目录，应加入 `.gitignore`，建议按下列方式组织：

```text
data/
├── input/<batch_id>/        # 原始合同 PDF；敏感输入，不提交版本库
├── runs/<batch_id>/         # 输入清单、运行配置快照、导出审核文件
├── artifacts/<contract_id>/ # 可选调试产物；默认不落盘
├── stores/                  # SQLite 文件与本地向量索引
└── logs/                    # 结构化日志
```

合同唯一标识建议为文件内容哈希；原始文件名仅作为展示信息。每项提取结果必须关联 `contract_id`、页码/坐标、原始证据、模型版本、提示词版本和字段库版本。

PDF 页面默认在内存中逐页渲染后直接传给 MLLM，不持久化页面图片。`artifacts/` 仅用于排障、失败复现或需要人工核对视觉输入时的显式开启模式；不得作为日常处理的前置依赖。

首期选用 SQLite 是为了零运维与事务一致性。未来服务化后，可将 `MetadataRepository` 和 `RunRepository` 替换为 PostgreSQL，而无需改变 `application` 或 `domain`。

## 8. 服务化演进

满足以下任一条件时再引入 FastAPI：需要外部系统上传合同、需要多人共享审核结果、需要任务状态查询，或需要持续处理新增合同。

届时新增而非迁移的部分为：

```text
interfaces/api/
├── routes/                  # upload、batches、reviews 等 HTTP 路由
├── schemas/                 # 请求与响应 DTO
└── dependencies.py          # 依赖注入与认证
```

较长的合同处理不应占用 HTTP 请求生命周期。路由只创建任务并返回 `batch_id` 或 `job_id`；后台 worker 仍调用 `ProcessBatch`。CLI 可直接调用同一用例，适用于本地调试、回归和离线批处理。

## 9. 首期边界

- 不预先创建 FastAPI、任务队列、容器编排或多租户代码。
- 不让领域模型返回 HTTP 状态码、框架异常或 ORM 对象。
- 不在模型客户端中写字段归并规则，也不在路由或 CLI 中拼装提示词。
- 不让 LangGraph、LlamaIndex 或 OpenAI SDK 的对象穿透到 `domain` 或 `application` 的公共接口中。
- Core 与 Attribute 的字段定义继续维护在 `description/fields/`；运行时由 `FieldCatalog` 读取，禁止散落硬编码在 Python 模块中。
