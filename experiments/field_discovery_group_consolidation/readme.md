# 字段发现组内收敛复现入口

## 用途

组内收敛已经接入
[`field_discovery_stage_one/run.py`](../field_discovery_stage_one/run.py) 的统一流水线，正常运行字段
发现时不再需要执行本脚本。本入口仅用于历史候选池复现、Prompt 调试或对同一个冻结
`candidate_pool.json` 做只读回归；它不重新运行 PDF 提取、不改写来源候选池、不写
Discovery/Production YAML，也不直接进入正式字段目录。

历史候选池不会被新门禁自动“洗白”：如果旧候选含位置化 `extraction_rule`、宽泛复合容器，
或把同一事实保存成多个身份，复现运行会明确失败或由全局语义门禁标记冲突。要验证前置语义
准入和关系图修复，必须重新运行完整 stage-one；本入口只能验证冻结输入之后的收敛行为。

一个组是 `same` / `related_distinct` 关系图的连通分量，只代表“候选应一起治理”，不是“一组
只能保留一个字段”，也不表示关系可以传递为同义。模型可以合并同义候选、
保留多个相关但不同的字段、拆解语义过宽的组，或淘汰低价值候选。

```text
candidate_pool.json（冻结输入）
  → 按 group_id 汇总候选字段
  ├─ 单候选组 → 程序确定性直通
  └─ 多候选组 → LLM 规划每个 candidate 的唯一去向
                 → 按 plan 逐字段生成定义
  → 程序按 output.type 逐字段编译正式定义和提取 JSON Schema
  → 跨组 field_id 门禁 → 固定覆盖/跨组重复/边界重叠语义门禁
  → 输出第二阶段验证与专家审核的字段定义草案
```

模型只读取字段定义和发现统计，不读取 PDF、页码、具体字段值或模型原始响应。

## 模型输出与门禁

多候选组分成两类响应：

- `GroupOwnershipPlan` 的 `reason → decision: refine_group`：本组收敛原则；理由固定以
  `因此 decision=refine_group` 收尾；
- `final_field_plans`：0 到多个字段身份计划，每项包含唯一的 `source_candidate_ids`、名称、含义
  和边界，但不生成复杂 output；
- `discarded_candidates`：未保留候选及原因，每个原因固定以
  `因此 disposition=discarded` 收尾；
- 每个 plan 的 `FinalFieldDefinitionSuggestion`：只生成一个被程序绑定来源的最终定义，模型无权
  再分配 candidate。

单候选组没有可归并关系，直接沿用已通过前置门禁的候选定义，并确定性生成提取 Schema；不会
调用模型，也不会虚构别名或排除概念。多候选组的模型只生成基于 `output.type` 的递归字段描述，
不生成 `nullable`、`required`、`additional_properties`、`anyOf` 等 JSON Schema 细节。程序确保
每个输入 `candidate_id` 恰好有一个去向，按类型编译每个最终字段，并生成单字段后续提取 JSON
Schema。来源候选名称会被程序确定性补入 aliases；同一分量保留多个字段时，兄弟字段名称会
补入彼此 not_meaning。examples 不在本阶段编造。字段契约、候选覆盖、跨组重复 `field_id` 或
全局语义门禁不通过时，不生成可推广草案；所有 JSON 解析和程序门禁失败都会携带脱敏原因重试
一次，并保留 finish reason/token 指标而不保存 raw response。

模型输入使用语义卡而非原始 JSON。第一阶段为空的 `aliases`、`not_meaning`、`examples` 不展示；
模型也不负责生成这三个字段，程序只从来源候选名称/既有边界和兄弟字段确定性补齐，examples
留给后续真实验证和专家治理。

最终 `extraction_rule` 必须是跨合同语义规则，不能包含页码、条款号、固定章节或当前合同原句。
位置化规则会触发与统一流水线相同的程序门禁，失败原因会在组级调用中反馈给模型重试一次。

## 历史产物复现

```bash
python experiments/field_discovery_group_consolidation/run.py \
  --source-run experiments/outputs/field_discovery_stage_one/20260803T043749915744Z
```

可选参数：

- `--max-members-per-group`：单次治理允许的每组最大字段数，默认 `20`；超过上限会明确失败，
  不会截断候选定义；
- `--max-validation-retries`：单组字段定义或候选覆盖门禁失败后的重试次数，默认 `1`。

脚本末尾也保留 IDE 编辑区；显式 CLI 参数优先。

## 输出与边界

每次运行写入 `experiments/outputs/field_discovery_group_consolidation/<run-id>/`：

```text
manifest.json                # 输入快照、门禁结果与汇总指标
stage.log                    # 过程摘要，不含模型原始输出
group_refinements.json       # 每组最终字段、候选去向与局部提取 Schema
global_semantic_gate.json    # 固定覆盖、跨组同义和边界重叠门禁
field_definition_drafts.json # 扁平化的有效最终字段定义草案
refinement_plan.json         # 是否可进入第二阶段验证的整体计划
```

`field_definition_drafts.json` 是第二阶段动态提取与统计的候选输入，不是正式字段目录。专家审核
通过且完成第二阶段验证后，才允许由独立治理动作写入 Discovery 或 Production Core/Attribute。
人工分析运行结果时，必须在该运行目录追加 `analysis.md`。
