# Attribute 双运行模式设计

> 状态：双模式配置、应用用例、LangGraph 拓扑、0 Core 策略、结果协议和固定 Attribute 提取
> 算法已经实现；Attribute 使用 `0.3/draft` 初始目录逐字段提取。字段发现两阶段已迁入正式
> `FieldDiscoveryService` 与批次用例，CLI 可在一次运行中完成候选收敛和全合同集回扫统计。

---

## 快速导航

- [术语与边界](#2-术语与领域边界)：Candidate、固定 Attribute 与模式职责。
- [字段发现](#3-运行模式一字段发现)：发现阶段的产物和限制。
- [正式生产](#4-运行模式二正式生产)：固定字段提取与最终门禁。
- [隔离与迁移](#5-两种模式的强制隔离)：禁止交叉的状态、副作用和演进约束。

---

## 1. 设计目的

Attribute 是连接模型能力与业务元数据建模的关键模块，但它在项目的不同阶段承担两种
性质不同的任务：

1. 在字段体系建立或演进阶段，从合同语料中开放发现现有字段目录未覆盖的业务概念；
2. 在正式生产阶段，严格按照专家确认的 Attribute 定义提取合同内容。

这两种任务分别遵循开放世界和封闭世界假设，输出、质量目标和副作用边界均不相同，不能
由一个含糊的 Attribute 节点在运行时自行决定行为。项目必须通过显式运行模式构造不同的
工作流拓扑。

> **核心决策：** 运行模式是一个显式、互斥的能力集合，而不是由多个布尔开关临时拼出的配置。`discovery` 只治理候选；`production` 只提取已确认字段。

两种模式复用同一份 PDF prepare 能力，但在 prepare 之后进入不同管线；生产主线保持既有
职责，只新增发现管线：

```text
合同 PDF → prepare
  ├─ discovery：shared_core_or_empty → shared_attribute_or_empty
  │              → field_discovery → candidate grouping → batch statistics
  └─ production
       ├─ shared_core → shared_attribute_or_empty
       ├─ Clause
       └─ Abstract
            → 汇总 → 专家终审 → 正式存储
```

图中的 shared 表示 Core 与固定 Attribute 的提取实现由两种模式共同复用，但目录快照严格
隔离：production 读取正式目录，discovery 读取独立初始目录。两种模式都保持 Core 完成后再
执行 Attribute；Discovery 在固定 Attribute 完成后才开放发现新字段。

生产图按上述真实业务依赖执行：Clause、Abstract 在 `prepare` 后与 Core 并发，Attribute 等待
Core 结果。每次正式模型调用共享 `models.mllm.max_concurrent_requests`（默认 3）配额，因此
并发不会绕开本地 vLLM 的资源边界。新增发现能力不得改变生产结果 DTO、终审或入库协议。

---

## 2. 术语与领域边界

| 术语 | 定义 |
| --- | --- |
| Core Definition | 经专家确认、跨合同普适且业务关键的固定字段定义。 |
| Attribute Definition | 经专家确认、通常面向特定合同类型或业务领域的固定扩展字段定义。 |
| Attribute Candidate | 字段发现模式产生的字段提案，尚不是正式字段定义。 |
| Attribute Extraction | 正式生产模式依据固定 Attribute Definition 得到的合同字段值。 |
| Field Catalog | 带版本的 Core 或 Attribute 正式字段目录。 |

Attribute Candidate 与 Attribute Extraction 必须使用不同的数据模型。候选记录可以包含
发现频次、来源合同、相似字段和审核状态；正式提取结果只包含固定字段的值、状态和证据，
不得混入候选治理信息。

> **对象边界：** Candidate 是待审核的字段知识；Extraction 是某份合同的固定字段结果。两者不能共用 DTO，也不能相互写入对方的持久化边界。

候选经专家审核后有四种主要去向：

```text
Attribute Candidate
  ├─ 跨合同普适、关键且定义稳定 → Core Definition
  ├─ 领域特有但具有稳定价值       → Attribute Definition
  ├─ 与已有字段重复                 → Merge
  └─ 价值不足或不可稳定定义         → Reject / Archive
```

模型负责提出和整理候选，不能自动把候选写入正式 Core 或 Attribute 目录。

---

## 3. 运行模式一：字段发现

### 3.1 模式标识与目标

模式标识为 `discovery`。该模式用于项目冷启动、引入新合同类型以及周期性发现字段体系的
覆盖缺口，主要目标是高召回地形成可审核的字段提案，而不是生成可直接入库的正式合同对象。

Discovery 使用与生产目录物理隔离的 Core/Attribute 目录作为“已覆盖字段空间”。模型需要
理解固定字段的名称、业务含义、别名、排除语义和输出结构，避免重复提出同义字段。两个目录
均允许为空；当一个字段都没有时，同一接口退化为完全开放的 Schema 冷启动，不需要另建一套
特殊流程。

固定 Discovery Core/Attribute 只作为 Prompt 约束并执行各自的固定字段提取，不进入新候选
向量池，也不参与候选分组。它们在批次启动时被冻结，运行过程不得修改。

> **事实来源：** Discovery 模型必须直接阅读原始 PDF；固定字段结果仅提供合同上下文和“已覆盖字段空间”，不能替代合同原文。

### 3.2 单合同与合同集流程

字段发现分为单合同观察和合同集治理两个层级：

```text
单份合同
  → 计算 document_id 并读取原始 PDF
  → 读取独立 Discovery Core/Attribute 目录（均允许为空）
  → 依次提取固定 Core、固定 Attribute
  → 基于原文、固定约束和提取结果提出最多 5 个新字段
  → 程序执行结构编译与逐候选并发语义准入门禁
  → 仅在本批次新候选池中召回 Top 5
  → LLM 对 Top 5 全量执行 same/related_distinct/unrelated 判别
  → 程序确定候选身份并更新关系图治理分量

固定合同集
  → 逐合同增量建立并冻结新候选字段池
  → 新候选字段回扫完整合同集
  → 统计不同合同命中数和各失败状态
  → 按命中率、证据质量和定义稳定性评分
  → 生成专家审核队列
```

发现模型必须直接读取合同原文。只把 `core_result` 传给发现服务会丢失尚未建模的信息，无法
完成真正的字段发现。Core 与固定 Attribute 结果只能作为合同上下文和覆盖约束，不能替代
PDF 输入。完整两阶段算法见
[字段发现两阶段工作流](field-discovery-workflow.md)。

### 3.3 禁用功能

`discovery` 模式必须禁用 Clause 和 Abstract。禁用通过构图时不注册相应节点实现，不能先
执行模型调用再丢弃结果，也不应开放互相独立的布尔开关形成无效组合。

> **禁止旁路：** 不得以“先调用、后丢弃”的方式模拟禁用，也不得通过自由组合开关创建未定义的运行模式。

正式 `discovery` 入口由单合同图和批次父图共同组成：

```text
单合同图：START → prepare → extract_core → extract_attributes
                → discover_fields → finalize → END

批次父图：START → stage_one_discovery
                  → stage_two_statistics → END

第二阶段子图：START → Send(单合同 × 单冻结字段)
                    → extract_candidate_field
                    → calculate_candidate_statistics → END
```

批次级用例按合同顺序复用一个候选池，候选冻结后再动态并发回扫完整合同集。历史
独立实验 [`field_discovery_stage_one`](../../experiments/field_discovery_stage_one/readme.md)
保留为复现入口并反向复用正式实现，不再维护独立算法；专家晋级和目录版本治理仍在正式
运行之外完成。

### 3.4 候选最小信息

每个候选至少应包含以下三类信息：

#### 身份与定义

- 稳定的候选标识、建议 `field_id`、名称、业务含义和别名；
- 建议的递归 `output` 定义与提取规则；

#### 证据与关系

- 来源 `document_id`、页码或坐标等证据定位；不保存正式合同字段值；
- 与同批次新候选的 Top 5 三分类结果、复用身份、关系边和治理分量；
- 未被固定 Discovery Core/Attribute 覆盖的新颖性说明；

#### 治理与可追溯性

- 模型、Prompt、字段目录和批次版本；
- 不同合同出现数、总观察次数、置信度与审核状态。

同一合同中的重复表述只能增加观察次数，不能重复增加“不同合同出现数”。所有候选必须有
可回到原 PDF 的证据定位；只有推断、没有合同证据的概念不得进入审核队列。具体字段值只可
在验证阶段临时存在，不作为 Discovery 业务数据长期保存。

---

## 4. 运行模式二：正式生产

### 4.1 模式标识与目标

模式标识为 `production`。该模式使用带版本的 Core 和 Attribute 正式目录，稳定生成合同的
结构化 Core、Attribute、Clause 和 Abstract 结果。

生产模式必须配置至少一个 Core Definition。0 Core 是字段模型尚未建立的冷启动状态，只在
`discovery` 中合法；`production` 必须在 PDF 渲染和模型连接前拒绝这种配置。

正式 Attribute 目录允许为空。空目录表示当前没有需要生产提取的扩展字段，工作流构图时
直接跳过 Attribute 节点，并在最终合同协议中返回 `attribute: []`；这不是错误，也不应调用
模型。目录非空时才执行固定 Attribute 提取。

生产模式遵循封闭世界原则：

- Core 只能输出 `core.yaml` 已定义的字段；
- Attribute 只能输出 `attribute.yaml` 已定义的字段；
- 模型不得创建、改名、拆分或合并字段；
- 未找到内容时按字段定义输出明确的空值或状态，不得删除字段以掩盖缺失；
- 每个结果必须记录 Core 与 Attribute 的 Schema 版本；
- 正式处理期间不得修改字段目录或候选统计。

> **启动门禁：** `production` 的 0 Core 必须在 PDF 渲染和模型连接前失败；空 Attribute 则是合法配置，必须跳过节点并稳定返回 `attribute: []`。

### 4.2 生产流程

```text
原始 PDF
  → 计算 document_id 并构建共享多模态上下文
  → 三条业务分支
     ├─ Core 固定字段提取 → Attribute 固定字段提取
     ├─ Clause 条款提取
     └─ Abstract 固定摘要生成
  → 各阶段校验与结果汇总
  → 专家终审
  → 确认后写入正式存储
```

如果 Attribute 字段集合由某个 Core 分类结果决定，可以在 Core 完成后选择一个固定且已
版本化的 Attribute Profile；选择完成后仍属于封闭式提取，不能临时发明 Profile 外字段。
如果 Attribute 定义不依赖 Core 结果，编排器可以并行或顺序执行两类字段提取，调度方式
不得改变业务结果。

Clause 和 Abstract 在生产模式中启用，继续保持各自独立的 Schema、Prompt 和质量门禁；
它们不承担 Attribute 发现或字段目录演进职责。

---

## 5. 两种模式的强制隔离

| 约束 | `discovery` | `production` |
| --- | --- | --- |
| 主要目标 | 建立和完善字段模型 | 按既定模型提取合同内容 |
| Core 目录 | 已知覆盖空间，允许为空或非空 | 固定、带版本且至少包含一个字段 |
| Attribute 行为 | 开放发现候选 | 非空时按固定定义提取；空时跳过并返回 `[]` |
| Clause | 不构图、不调用 | 启用 |
| Abstract | 不构图、不调用 | 启用 |
| 允许产生新字段 | 允许产生候选 | 禁止 |
| 允许修改正式目录 | 运行过程禁止；仅专家审核后的治理用例可改 | 禁止 |
| 输出对象 | `FieldDiscoveryResult` | `ContractProcessingResult` |
| 主要质量目标 | 召回、去重、证据和可审核性 | 准确、完整、确定性和 Schema 合规 |

两种模式不得共用同一个宽泛结果 DTO，也不能用空数组判断当前处于何种模式。调用方必须
显式选择运行模式；发现结果通过独立 `FieldDiscoveryResult` 回显 `mode=discovery` 和字段
Schema 版本，生产 `ContractProcessingResult` 保持既有对外协议以避免索引和 API 迁移。

> **调用方责任：** 调用方必须显式传入模式；不能通过结果是否为空反推模式或改变后续处理逻辑。

---

## 6. 服务与编排设计

开放发现和固定提取应拆成独立服务，避免一个类同时承担元数据建模与合同值抽取：

```python
class FieldDiscoveryService(Protocol):
    async def discover(self, request: FieldDiscoveryRequest) -> FieldDiscoveryOutput: ...


class AttributeExtractionService(Protocol):
    async def extract(self, context: ExtractionContext) -> AttributeExtraction: ...
```

推荐由工作流工厂依据枚举构造不同拓扑：

```yaml
runtime:
  mode: discovery  # discovery | production
```

禁止使用 `enable_clause`、`enable_abstract`、`discover_attributes` 等多个布尔值自由组合；模式
本身就是经过验证的能力集合。应用层用例也应区分为：

- `DiscoverContractFields` / 后续 `DiscoverFieldsFromBatch`；
- `ProcessContract` / `ProcessBatch`；
- `ReviewFieldCandidates` / `ApplyFieldCatalogDecision`。

LangGraph 只负责拓扑和状态传递，不决定候选能否成为 Core，也不修改字段目录。字段归并、
频次统计和晋升规则属于领域策略；LlamaIndex 只负责相似字段召回，不能直接决定合并。

---

## 7. 版本、审核与副作用边界

### 7.1 审计与目录版本

- 每次发现运行记录输入合同集、模型、Prompt、Core 目录和 Attribute 目录版本；
- 候选归并必须保留来源、目标、判定理由以及变更前后定义；
- 专家审核是候选进入正式目录的唯一边界；
- 被接受的候选应生成新的目录版本，并触发字段定义校验和回归测试；

### 7.2 运行快照与副作用

- 生产任务只能读取任务开始时固定的目录快照，处理中不得感知目录更新；
- 正式抽取服务保持无本地文件副作用，实验产物和人工审核导出由各自边界负责保存。

---

## 8. 当前实现状态与迁移约束

当前 `data/definitions/attribute.yaml` 为 `0.3/draft`，包含 10 个专家预置字段，具体定义和
边界见 [Attribute 字段设计](../fields/attribute/attribute.md)。这些字段用于下一阶段固定 Schema
提取器的真实合同验证，不是 discovery 自动生成的结果。production 会在非空目录时注册逐字段
提取器。字段成功调用后必须返回合法业务终态；初次调用和一次纠错都失败的字段可以省略，但
必须在处理诊断中标记为技术失败，不能用空列表或 `not_found` 伪装完整成功。

### 已完成

1. `RuntimeMode` 和 `settings.runtime.mode` 显式模式配置；
2. `ProcessContract` 与 `DiscoverContractFields` 独立用例及结果 DTO；
3. production 与 discovery 两套 LangGraph 拓扑，发现图不注册 Clause/Abstract；
4. `EmptyCoreExtractionService`，发现模式 0 Core 时返回 `{}` 且 Core 模型调用数为 0；
5. 生产模式 0 Core 启动前门禁和字段目录空状态一致性校验；
6. `FieldDiscoveryService`、`FieldDiscoveryRequest` 与 `FieldDiscoveryOutput` 接入协议；
7. CLI `--mode` 与 production/discovery 拓扑、0/非0 Core 的回归测试。
8. 生产 Attribute 空目录条件构图：不注册 Attribute 节点且稳定返回空结果。
9. 专家预置的 `0.3/draft` Attribute 初始目录及递归字段定义。
10. 固定 Attribute 逐字段提取、动态 JSON Schema、单字段一次纠错，以及保留成功字段的局部
    降级和失败诊断。
11. Core Step 1 合同理解地图和成功 Core 简洁上下文的内存级复用；原始 PDF 仍为事实来源。
12. 正式字段发现默认服务：单候选并发准入、批次向量池、逐对关系判断和组级收敛。
13. `DiscoverFieldsFromBatch` 第二阶段：冻结字段 × 去重合同的逐字段并发回扫与确定性频率统计。

### 尚未实现

1. 候选字段的专家审核交互与目录晋级用例；
2. Attribute Profile 的选择和版本治理。

在上述能力完成前，不得通过移除空目录校验或让模型自由返回任意键的方式“临时支持”
Attribute；这会绕过字段治理边界，并使生产结果无法建立稳定索引。

> **迁移底线：** 未实现的能力必须显式失败或保持既定空目录语义；不能放宽字段契约来伪造支持。
