# 合同信息化处理工作流

> 状态：已实现的正式工作流。Core、Clause 和 Contract Summary 同时保留独立实验入口，
> 统一图通过适配器复用同一算法实现、共享页面图像和模型客户端。
> 本文件只描述 `production` 模式；字段发现模式见
> [`attribute-operating-modes.md`](attribute-operating-modes.md)。

---

## 快速导航

- [总体流程](#2-总体流程)：自动化处理与专家终审的衔接。
- [自动化线路](#3-自动化线路职责)：Core、Attribute、Clause 与摘要的职责划分。
- [执行关系](#4-共享输入与执行关系)：共享输入、并发和隔离约束。
- [校验与落盘](#5-自动校验边界)：自动门禁、专家最终确认与输出接口。

---

## 1. 用途

本工作流把一份原始合同 PDF 转换为经过专家最终校验、可正式检索和使用的合同信息。
自动化负责提取、结构化和摘要生成；专家直接核对并修正自动化最终结果；专家确认后，
结果一次性落盘。

本工作流严格按带版本的 Core 和 Attribute 目录提取固定字段，不执行字段发现、候选归并或
字段目录更新。工作流不建设自动跨产物一致性校验、冲突裁决、审核驳回或人工触发局部重跑。
Core、Attribute、Clause 和 Abstract 之间的事实一致性由专家在最终校验时统一确认。

Core、Clause 与 Abstract 保持阶段硬门禁。Attribute 使用字段级门禁：单字段初次提取和一次
纠错均失败后只省略该字段，成功字段继续汇总；处理元数据明确记录局部失败，专家据此复核或
重试。技术失败不等于合同未记载，因此不能自动填成 `not_found`。

生产模式要求 Core 目录至少包含一个字段。依赖组装阶段会在 PDF 渲染和模型连接前拒绝
0 Core；零 Core 冷启动只属于 discovery 模式。

> **工作流边界：** 自动化各自保证本线路的结构与业务规则；跨产物事实一致性和最终取舍只由专家对照原始 PDF 确认。

---

## 2. 总体流程

```text
原始 PDF
  → 计算原始文件 SHA-256 document_id
  → 统一渲染页面并构建共享多模态前缀
  → 三条业务线路
     ├─ 线路一：Core 固定字段提取 → Attribute 固定字段提取
     ├─ 线路二：Clause 提取
     └─ 线路三：Abstract 生成
  → 汇总通过各自内部校验的自动化最终提取物
  → 专家对照原始 PDF 进行最终校验并直接修正
  → 专家确认后一次性落盘
```

三条线路中只有线路一存在 Core → Attribute 的数据依赖；后续可以根据 Core 分类结果选择
一个固定、带版本的 Attribute Profile，但选择后仍只能提取 Profile 内的字段。Attribute 不得
借此发现或返回目录外字段。Clause 与 Abstract 不消费 Core 或彼此的模型结果，因此在
`prepare` 后与 Core 同时启动，最终与 Core/Attribute 链路汇合。三条线路共享输入和计算
前缀，但不能合并 Schema、业务校验或失败边界。

Attribute 正式目录允许为空。构图器在空目录时不注册 Attribute 子图，Core 完成后直接进入
后续生产节点，最终协议仍输出 `attribute: []`；目录非空时才注册固定字段提取子图。

正式拓扑由 `LangGraphWorkflowFactory.build_contract_processing` 组装。`prepare` 后扇出
`extract_core`、`extract_clauses` 和 `extract_abstract`；Core 成功后才触发
`extract_attributes`；`finalize` 显式等待所有已注册分支。为避免三分支演变为无界请求，所有
正式 MLLM 调用共享 `models.mllm.max_concurrent_requests` 配额，当前默认值为 3。

> **并发边界：** 并发只发生在不存在事实依赖的业务线路之间；共享请求配额限制资源使用，不能通过伪造业务依赖来代替资源控制。

---

## 3. 自动化线路职责

### 3.1 Core 与 Attribute

Core 按字段目录逐项提取高价值、可稳定过滤的结构化字段，并保留状态、原文证据和
规范值。Attribute 按正式目录逐项提取已确认的扩展字段，并使用相同的固定 Schema、状态、
证据和版本约束。生产模式不产生 Attribute Candidate，也不执行候选归并与统计。

Core 和 Attribute 的规范源分别为
[`core.yaml`](../../data/definitions/core.yaml) 和
[`attribute.yaml`](../../data/definitions/attribute.yaml)。字段增长通过规范源驱动，不在
工作流编排代码中硬编码字段关系。

当前 Attribute 规范为包含 10 个专家预置字段的 `0.3/draft` 目录。固定字段提取服务按目录
逐字段执行，且复用 Core Step 1 的合同理解地图和成功 Core 的简洁规范值作为定位辅助；两者
都不能替代原始 PDF 证据。目录显式为空时生产图才跳过 Attribute 节点并返回 `[]`。
开放字段发现由独立 `discovery` 工作流承担，不接入本生产节点。

> **事实来源：** Core 的理解地图和成功字段值只用于定位；每个 Core 或 Attribute 结论仍必须回到原始 PDF 核验。

### 3.2 Clause

Clause 独立提取合同中的规范性条款，保持原始顺序、原文内容和物理页码。Clause 不为
Abstract 预先筛选材料，也不与 Core 建立自动映射或一致性比较。输出规范源为
[`clause.yaml`](../../data/definitions/clause.yaml)。

### 3.3 Abstract

Abstract 直接读取完整原 PDF，生成固定六栏目合同摘要。它不使用 Core、Attribute 或
Clause 作为生成输入，避免上游遗漏成为摘要的信息瓶颈。

Abstract 继续执行自身的 JSON Schema、栏目业务校验和自动局部重试。这些属于摘要节点
内部的结果质量控制，不是跨 Core、Clause 和 Abstract 的一致性校验。摘要协议见
[`contract-summary.md`](../contract-summary/contract-summary.md)。

---

## 4. 共享输入与执行关系

同一合同只计算一次 `document_id`，并只渲染一次页面。Core、Clause 和 Abstract 复用同一
组内存页面以及完全一致的公共 Prompt 前缀，具体规则见
[`mllm-prompt-prefix.md`](mllm-prompt-prefix.md)。

三条业务线路在生产图中实际并发；这不表示其内部字段或条款调用无界并发。Core、Clause、
Abstract 和未来 Attribute 的每次模型调用都必须获取共享请求配额。当前默认配额为 3，部署
方可依据真实 vLLM 吞吐、KV Cache 和显存观测调整；该资源限制不得通过伪造业务依赖实现。

> **共享而不耦合：** 三条线路共享 `document_id`、页面和公共 Prompt 前缀，但保持独立的 Schema、业务校验、失败处理和结果协议。

---

## 5. 自动校验边界

每条线路只保证自身输出满足其 Schema、业务规则、证据和页码要求。正式工作流不增加
以下自动化步骤：

- 不比较 Abstract 与 Core；
- 不比较 Abstract 与 Clause；
- 不比较 Core 与 Clause；
- 不调用模型进行跨产物冲突发现或裁决；
- 不保存冲突候选、裁决过程或修复前后的业务版本。

这一取舍基于当前工作流必经专家终审。重复增加一层模型审查会扩大调用成本和维护
范围，但不能代替专家对原始 PDF 的最终事实确认。

实验代码可按新功能需要保存原始响应、校验错误或重试材料；它们是研发产物，不属于正式
合同信息的业务落盘内容。当前已迁移算法的薄实验入口默认只保存最终 `result.json`。

> **自动校验止于线路内部：** 不得把研发期的跨产物实验比较升级为生产自动裁决；发现矛盾时仍由专家审阅原始 PDF 后决定。

---

## 6. 专家最终校验

专家界面或审核载体应同时提供：

- 原始 PDF；
- Core 和 Attribute 最终候选；
- 按原文顺序排列的 Clause 最终候选；
- 固定格式的 Abstract 最终候选；
- 各结果已经携带的证据和物理页码。

专家对照 PDF 检查事实、跨产物一致性和表达质量，必要时直接修改当前最终候选，然后
确认保存。首期不设置“驳回”“返回自动处理”或“人工触发局部重跑”动作，也不建立
审核修订链路。专家确认后的内容就是本次落盘的正式结果。

> **唯一确认点：** 专家确认是自动候选成为正式合同信息的唯一业务边界；未形成合法最终候选的任务不能进入终审。

---

## 7. 落盘接口与内容

专家确认是唯一业务落盘边界。前端回传的待入库包络至少包含：

### 7.1 确认包络

```yaml
document_id: <原始 PDF 文件字节的 SHA-256>
review:
  reviewer: ""
  reviewed_at: <带时区的 ISO 8601 时间>
  comment: ""
result:
  document_id: <与外层一致的 SHA-256>
  source_name: ""
  core: {}
  attribute: []
  clause: []
  abstract:
    sections: {}
    text: ""
  processing: {}
```

`result` 与 production 单文件 CLI 返回的 JSON 主体完全一致。前端不删除结果字段；专家不
认可的字段通过将最终 `value` 置空表达，空字段只在后端生成 Elasticsearch 稀疏投影时排除。

### 7.2 正式存储与检索投影

- 原始 PDF 文件按 `document_id` 关联落盘；
- 专家确认后的元数据和摘要写入 Elasticsearch；
- Abstract 正文生成面向语义检索的 `abstract_vector`；后续独立入库模块还必须为原始合同
  生成面向重复召回的 `document_visual_vector`，两者与同一 `document_id` 关联但用途不同；
- 合同名称和产品名称在非空时分别生成字段级文本向量；对方公司名称只保存为 SmartCN
  文本元数据，不生成 dense vector；Core、Attribute 和 Clause 的其余结构化元数据不参与摘要向量计算；其中 Elasticsearch
  检索投影只保留 `found` 且非空的 Core、Attribute 及对象子字段，缺失字段合法省略；
- 完整终审对象仍保留所有字段状态、原文与理由，不能因检索投影裁剪而丢失审计信息；
- `contract_number` 是可空、可重复业务字段，不能替代 `document_id`。

> **完整审核对象 / 稀疏检索投影：** 终审对象保留全部状态、原文与理由；Elasticsearch 只接收实际存在的可检索事实，缺失字段不能用 `null` 或空对象占位。

具体 Elasticsearch mapping、PDF 目录或对象存储位置以及确认写入接口在持久化模块实现时
另行定义。无论采用何种基础设施，都不能在专家确认前把自动化候选当作正式合同信息。
入库阶段的哈希精确判重、VL-Embedding 相似召回、VL 模型精判、专家确认和安全替换协议见
[合同终审入库与多模态判重设计](contract-ingestion-deduplication.md)；该能力当前仅完成
设计，尚未接入正式代码。

---

## 8. 依赖与注意事项

- 运行模式及 Attribute Candidate/Definition/Extraction 的隔离依赖
  [`attribute-operating-modes.md`](attribute-operating-modes.md)。
- 身份协议依赖 [`document-identity.md`](document-identity.md)。
- 字段、条款和摘要策略依赖 `data/definitions/` 下的机器可读规范源；`description/` 只保留
  说明文档。
- MLLM、Embedding 和 Elasticsearch 连接参数应由统一配置加载，不能散落在业务流程中。
- 专家修改后的对象仍须通过对应最终 Schema，避免将无法索引的结构写入存储。
- 自动处理阶段出现技术错误或某条线路没有形成合法最终候选时，任务不能进入专家终审；
  这属于处理未完成，不是审核驳回流程。
