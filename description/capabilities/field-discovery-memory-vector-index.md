# 字段发现批次内存向量索引

> **定位：** 这是 discovery 批处理的临时召回组件，只为新字段候选构造局部比较集合；它不保存长期数据，也不作字段身份或分组裁决。

---

## 模块用途

本模块只服务于 `discovery` 批处理中新字段候选之间的相似度召回。它将当前批次已经获得独立
身份的新候选转换为向量，在进程内维护临时 LlamaIndex 索引，用于为后续新候选构造局部 Top 5
比较集合；不索引固定 Discovery Core/Attribute，不承担字段身份或分组结论，也不保存长期
业务数据。

---

## 主要职责

- 将新候选的名称、含义和输出结构正向视角转换为 Embedding；
- 使用 LlamaIndex `SimpleVectorStore` 增量维护当前批次候选内存索引；
- 融合多视角排名并返回最多 5 个领域候选，不向应用层泄漏 LlamaIndex Node；
- 校验重复字段标识、空向量、向量维度不一致和非法查询参数。

> **生命周期：** 一个实例对应一个批次，从空池开始、随新身份增量增长，并在批次结束后整体释放。

---

## 对外接口与使用方式

第一大步统一实验流水线中的 `CandidateVectorPool` 位于
[`experiments/field_discovery_stage_one/discovery.py`](../../experiments/field_discovery_stage_one/discovery.py)。
它通过三个 `SimpleVectorStore` 分别维护名称、含义和结构视角；`top_matches(...)` 返回已融合并
去重的领域 `CandidateMatch`，不向上层泄漏 LlamaIndex Node。调用方只传本批次已获独立身份的新
候选，固定 Discovery Core/Attribute 始终不进入池中。

> **接口边界：** 上层只接触领域 `CandidateMatch`；LlamaIndex 的 `TextNode`、向量存储和相似度实现不向应用层泄漏。

第一个门禁通过的候选直接写入空池；后续候选先查询当前池，再根据完整 Top 5 的 LLM 三分类和
确定性规则决定复用或创建身份。全部 `same` / `related_distinct` 边用于维护治理关系图连通分量，
不再只选择一个最高分锚点组。一次批处理结束即释放整个对象，
不为每份合同创建独立索引。正式 `FieldDiscoveryService` 尚未迁移，生产 discovery 入口仍会在
没有服务时明确失败。

> **当前状态：** 该能力已在第一大步实验流水线实现；正式 `FieldDiscoveryService` 尚未迁移，生产 discovery 入口会明确拒绝执行。

---

## 关键实现与设计决策

- **仅驻留内存**：不读取或写入 Elasticsearch，不配置专用持久化索引，也没有索引清理、
  迁移和跨批次一致性负担。
- **固定约束不入池**：Discovery Core/Attribute 由 Prompt 和新颖性门禁负责，不生成候选池
  向量，避免把不可变字段与新候选生命周期混淆。
- **批次增量池**：一个实例只代表一次批次，从空池开始随新身份增量增长；批次之间不复用。
- **多视角正向语义**：第一步使用 `name`、`meaning` 和程序编译后的 `output` 结构摘要；
  此时不存在经治理的候选别名，
  `not_meaning` 与否定规则只交给 LLM 精判，不进入正向向量。
- **召回不裁决**：Top 5 只提供局部比较集合；`same`、`related_distinct`、`unrelated` 由模型
  判断，身份和关系图分量由应用层确定性规则决定。RRF 分数是排名融合值，不是 0~1 概率；产物
  同时记录名称、含义、结构三个视角的原始相似度和名次，以便基于人工标签校准阈值。
- **实验隔离**：LlamaIndex `TextNode` 和 `SimpleVectorStore` 当前仅存在于第一大步实验；正式
  应用层迁移时才会通过领域端口隔离该实现细节。

> **裁决边界：** 向量召回只缩小对比范围。`same`、`related_distinct`、`unrelated` 由模型判断，身份与关系图分量由确定性规则维护。

---

## 依赖与注意事项

- 依赖 `llama-index-core>=0.14,<0.15`，不需要 Elasticsearch VectorStore 插件；
- 候选写入向量和查询向量必须使用同一 Embedding 模型、指令和维度；
- 内存占用随字段数和向量维度线性增长，适用于当前批次字段目录，不用于大规模合同正文库；
- 进程退出或搜索器对象释放后索引不可恢复，这是批处理设计的预期行为。
- 增量多视角候选池和冻结后的组级收敛已在同一实验流水线实现；第二阶段全合同集回扫、候选
  统计和正式应用服务仍待实现，
  完整协议见 [字段发现两阶段工作流](../architecture/field-discovery-workflow.md)。

> **存储原则：** 不读取、不写入 Elasticsearch，也不配置跨批次持久化索引；进程退出后索引不可恢复正是该批处理设计的预期行为。
