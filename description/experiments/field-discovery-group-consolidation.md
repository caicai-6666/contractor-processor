# 字段发现统一流水线的组级收敛模块

> **状态：** 算法已迁入正式 `FieldDiscoveryService`；本实验入口仅用于历史产物复现，代码反向复用 `src/contract_processor/infrastructure/field_discovery/`，且不修改字段目录。

---

## 模块用途

第一大步的 `group_id` 是关系图连通分量的组织键：全部 `same` / `related_distinct` 边把应一起
治理的候选语义族连通，但该组不等价于一个最终字段，也不声明传递同义。统一流水线在候选池
冻结后把每个分量作为治理单元，形成可进入第二阶段验证的最终字段定义草案。

---

## 职责与接口

正常入口为：

```bash
python experiments/field_discovery_stage_one/run.py --input-dir data/input
```

一次运行会在同一目录产生 `candidate_pool.json`、`candidate_relation_graph.json`、
`group_refinements.json`、`global_semantic_gate.json`、`field_definition_drafts.json` 和
`refinement_plan.json`。单候选组由程序直接复用候选定义，不调用模型；多候选组先用一次轻量
调用规划每个候选的唯一去向，再按最终字段逐个调用生成定义。每个字段描述随后经程序校验，并由 `output.type` 编译器及
`build_field_extraction_schema` 确定性生成正式定义和单字段抽取 JSON Schema。

历史运行复现仍可执行
`experiments/field_discovery_group_consolidation/run.py --source-run <stage-one-run-dir>`；该入口与统一
流水线调用同一个 `service.py`，不存在两套组级算法。

---

## 关键决策

- 一个关系图分量可输出 0 到多个最终字段；不能因连通性而把交货、付款、期限等不同业务字段强行
  合并为一个字段。
- 第一段 `GroupOwnershipPlan` 只生成连续的 `field_plan_01...`、来源候选、字段身份含义与边界；
  每个 `candidate_id` 必须且只能被一个 plan 吸收，或明确标记为 `discarded`。遗漏、重复归属和
  组外 ID 会在复杂 output 生成前触发门禁失败。
- 第二段按 plan 单独生成字段定义，来源 candidate 由程序绑定，模型无权重新分配。因此一次
  字段定义失败只需针对该 plan 重试，不会重写整个组的候选所有权。
- 同 field_id 或同规范名称的候选必须进入同一个 plan（或只保留一个、明确淘汰其余）；程序拒绝
  把同一事实拆成“原文版/结构化版/分类版”等多个最终字段。
- 单候选组没有合并、拆分或淘汰关系，使用确定性直通；只有两个及以上成员的组才需要模型治理。
- `output` 是字段结构的唯一来源。模型只选择 `output.type` 并描述该类型所需的枚举项、对象
  子字段或数组元素；不得输出 `nullable`、`required`、`additional_properties`、`anyOf` 或抽取
  包络。程序按类型递归编译正式定义，再为每个字段单独生成 JSON Schema。
- `extraction_rule` 是跨合同语义规则，不得包含页码、条款号、固定章节、版式位置或当前合同
  原句。命中位置化表达时，组级门禁把清晰失败原因反馈给模型局部重试一次。
- 第一阶段不让模型生成空洞的 `aliases`、`not_meaning` 或 `examples`。收敛时程序从被吸收候选
  名称和既有别名确定性补齐 aliases；一个分量保留多个字段时，兄弟字段名称确定性补入彼此
  not_meaning。模型不生成这三个治理字段；examples 留待第二阶段真实验证，单候选直通不凭空补造。
- 输入字段以简洁语义卡展示；空治理列表、examples 和原始 JSON 不占用模型上下文。
- JSON Schema 解析、候选覆盖、规则和字段契约失败均携带清晰原因重试一次。失败日志只保存错误
  类型、简短原因、finish reason 和 token 指标，不保存模型原文。
- 跨组 `field_id` 唯一门禁后还必须执行全局语义门禁；固定字段覆盖、跨组同义或边界重叠都会令
  草案不可推广，必须经后续治理处理，程序不自动删除冲突字段。
- 全局门禁由程序逐字段绑定，模型每次只返回一个判断；它只比较紧凑的字段身份、含义、顶层类型
  和对象子字段，不读取 extraction_rule，避免把同段共现、触发依赖或履约相关误判成语义覆盖。
- 非 accepted 初判必须再做一次仅含当前/目标字段的聚焦复核；复核否决时最终恢复为 accepted，
  同时保留初判与复核的脱敏审计记录。
- 输入候选池、PDF、字段具体值、Discovery/Production YAML 和 Elasticsearch 均为只读边界。

> **收敛边界：** 连通分量是共同治理单元，不等于单一最终字段；一个分量可产生零至多个字段，候选所有权必须被程序完整校验。

---

## 依赖与注意事项

依赖统一流水线冻结的候选池、字段定义领域模型、YAML 字段契约解析器和本地 MLLM。结果仅用于
第二阶段动态提取、频次统计与专家审核；它不等同于正式 Attribute/Core 目录，也不改变候选
身份和分组历史。正常执行与产物布局见
[统一流水线 README](../../experiments/field_discovery_stage_one/readme.md)，历史复现见
[兼容入口 README](../../experiments/field_discovery_group_consolidation/readme.md)。

> **产物用途：** 草案只供第二阶段动态提取、频次统计与专家审核使用；不会改写正式 Attribute/Core 目录，也不会追溯修改候选身份与分组历史。
