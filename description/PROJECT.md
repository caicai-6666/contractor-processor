# 合同元数据发现 Agent 工作流：项目说明

## 1. 项目目标

本项目构建一个面向 PDF 合同的 Agent 工作流，从固定批次的合同中提取并持续演进结构化元数据。

工作流优先稳定、准确地抽取高价值且普适的 **Core（核心字段）**；在此基础上，发现合同特有或暂未标准化的 **Attribute（动态属性字段）**。Attribute 字段会被归并、计数并按频次交由人类专家审核，专家可决定将成熟字段升级为 Core。Core 字段将作为后续 RAG 检索的首要过滤条件，因此其正确性、规范性与可追溯性高于字段覆盖数量。

## 2. 范围与非目标

### 范围

- 输入：固定批次的 PDF 合同，包括文本型、扫描型及带签章的合同。
- 内容理解：通过本地 vLLM 使用配置的多模态模型；当前实验配置为 `Qwen3-VL-32B-Thinking`，对合同页图像进行理解和结构化提取。
- 输出：合同级结构化 Core 数据、发现的 Attribute 字段、完整 Clause 条款实例及字段库变更记录。
- 人机协作：导出按频次降序排列的 Attribute 描述文档，供专家审核与决定是否升级为 Core。

### 非目标（首期）

- 不在首期自动将 Attribute 提升为 Core；升级决定必须由人类专家作出。
- 不以替代合同审阅、法律意见或自动判断合同有效性为目标。
- 不追求为每一个低频、无检索价值的条款细节建立字段。

## 3. 核心术语

| 术语 | 定义 |
| --- | --- |
| Core | 跨多数合同稳定出现、业务重要且适合作为 RAG 前置过滤条件的字段。 |
| Attribute | 在部分合同中发现的动态字段；可能是低频特有信息，也可能在经专家审核后成长为 Core。 |
| Clause | 按合同原始顺序保留具有自有原文标题或可继承明确父标题的规范性条款，不混入主体、金额表或签署资料，也不作为 Core 筛选字段。 |
| 字段库 | 保存 Core 和 Attribute 定义、别名、提取规则、示例及统计信息的知识库。 |
| 字段归并 | 将新发现字段与字段库中的候选字段进行语义召回和大模型判定，决定复用、完善或新建。 |
| 一轮处理 | 对一个固定合同批次进行完整遍历、字段归并与统计导出的迭代。 |

## 4. Core 字段目录

Core 的机器可读规范源为 [fields/core/core.yaml](fields/core/core.yaml)，用于字段库加载和提示词注入；字段设计说明与专家审核入口为 [fields/core/core.md](fields/core/core.md)。在修改 Core 提取逻辑、提示词或字段库前，必须先阅读这两个文件。

Attribute 的机器可读规范源为 [fields/attribute/attribute.yaml](fields/attribute/attribute.yaml)，字段迭代与审核说明见 [fields/attribute/attribute.md](fields/attribute/attribute.md)。该字段库在每轮固定合同批次处理完成后统一更新。

Clause 的机器可读规范源为 [fields/clause/clause.yaml](fields/clause/clause.yaml)，结构、边界与后续设计见 [fields/clause/clause.md](fields/clause/clause.md)。Clause 与 Core、Attribute 并行提取，输出按原始顺序排列且具有自有原文标题或可继承明确父标题的条款列表；不生成条款摘要，也不自动将条款内容迁入 Core。

## 5. 字段定义模型

Core 与 Attribute 使用同一套字段定义；Attribute 在此基础上增加统计与审核状态。字段定义的基线结构如下：

```yaml
field_id: ""          # 唯一英文标识，例如 contract_number
name: ""              # 中文名称，例如 合同编号
meaning: ""           # 字段的业务含义
aliases: []            # 合同中可能出现的其他名称
not_meaning: []        # 易混淆、但不属于该字段的概念
output:
  type: ""            # string、date、number、boolean、enum、object、array
  format: null         # 规范化输出格式
  nullable: true       # 缺失时是否允许为 null
  example: null        # 规范化后的示例值
  required: []         # object 必填子字段；所有 properties 键原则上都必须出现
  additional_properties: false
  properties: {}       # object 的递归子字段定义
  items: null          # array 的递归元素定义
  values: {}           # enum 值及各值业务含义
extraction_rule: ""   # 提取条件、优先位置及判断规则
examples:              # 正确提取示例
  - source_text: ""
    output: null
```

Attribute 额外记录：发现次数、出现的不同合同数、首次/最近发现轮次或时间、来源合同标识、审核状态及归并历史。用于专家决策的频次应优先使用“出现的不同合同数”，避免同一合同中的重复表述抬高统计值。

字段定义中的 `output` 递归描述规范值结构。object 必须详细声明每个子字段的含义、类型、空值语义与提取规则，array 必须声明 items，enum 必须声明 values。运行时从该定义动态生成 JSON Schema，禁止用格式字符串或 `Any` 代替复杂值约束。

非 object Core 字段按 `raw_value/reason/status/value` 输出；object 字段细化到直属子字段，每个子字段也按 `raw_value/reason/status/value` 输出，对象外层只保留由程序确定性汇总的 `status` 和 `properties`。子字段额外支持 `out_of_scope`，用于保留存在但不属于采用口径的原文。结果不再生成根级 `reason`，判断摘要与其负责的字段或子字段直接绑定。完整规则见 [fields/core/core.md](fields/core/core.md)。

## 6. 字段相似度与归并策略

1. 将 `name`、`meaning`、`aliases` 组合为字段摘要，并向量化建立检索索引。
2. 新发现的候选 Attribute 以字段摘要检索字段库，召回语义相近的 Core 与 Attribute。
3. 大模型结合候选字段定义、来源文本和合同上下文作出判定：
   - **一致**：映射至已有字段；必要时补充其 `aliases`、`meaning`、`not_meaning`、提取规则或示例。
   - **相关但不一致**：保留为独立 Attribute，并记录与候选字段的差异，避免错误合并。
   - **完全无关**：新建 Attribute。
   - **无业务价值或无法可靠定义**：不建字段，但记录处理原因以支持审计。
4. 所有修改均应保留来源、理由和变更前后内容，保证字段库可追溯、可复核。

向量召回只负责缩小候选范围，不能直接决定字段合并；最终语义判断由大模型完成，并在低置信度或冲突时进入人工审核队列。

## 7. Agent 工作流

```text
固定合同批次
  → PDF 页面化
  → 配置的多模态模型进行内容理解与文本/版面提取
  → 基于 Core 定义抽取并规范化 Core 值
  → 并行：在 Core 覆盖范围之外发现候选 Attribute / 完整提取 Clause 条款
  → 字段摘要向量召回字段库
  → 大模型判定：复用 / 完善已有字段 / 新建 / 舍弃
  → 更新字段统计与审计记录
  → 导出 Attribute 审核文档（按不同合同出现次数降序）
  → 专家审核，必要时将 Attribute 升级为 Core
```

Core 定义除承担抽取任务外，也用于提供 few-shot 示例与“已覆盖的字段空间”，从而引导模型在剩余内容中发现有区分度的新 Attribute，而不是重复创建 Core 的同义字段。

## 8. 质量原则

- Core 优先：宁可将不确定字段保留在 Attribute 审核队列，也不要错误地污染 Core。
- 证据优先：每个提取值和字段演进均应能回溯到合同、页码/位置与原始文本或图像证据。
- 规范化输出：日期、金额、枚举等必须遵循字段的 `output` 定义；原文与规范化结果应同时保留。
- 去重统计：同一份合同同一字段只计入一次“不同合同出现数”。
- 可迭代：每一轮均保留输入批次、字段库版本、模型/提示词版本与产出，便于对比与回归验证。

## 9. 待确认事项

- [Core 字段目录](fields/core/core.md)中哪些字段需要保留、拆分或调整优先级。
- 合同唯一标识规则（建议基于文件内容哈希，并保存原始文件名）。
- 字段摘要的具体拼接模板、向量模型及相似度召回阈值。
- 大模型归并判定的置信度分级，以及何种分级必须转人工审核。
- Attribute 升级为 Core 的专家决策标准，例如不同合同出现数、业务重要性与定义稳定性。
- 专家审核产物的具体格式（Markdown、CSV 或两者）及字段库的持久化格式。

## 10. 架构文档

项目的分层边界、目标目录、本地运行方式和未来服务化路径见 [architecture/PROJECT_STRUCTURE.md](architecture/PROJECT_STRUCTURE.md)。在新增模块、持久化实现或外部接口前，必须先阅读该文档。
