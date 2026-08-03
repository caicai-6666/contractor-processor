# 字段发现第一大步统一流水线

本实验实现字段发现的第一大步，不进入第二阶段的全合同集字段效果统计。原“逐合同候选建池”
和“冻结候选池组级收敛”已经合并为一次运行；批量读取 `data/input/` 中的 PDF 后，按以下
五个业务节点连续执行：

```text
1. 共享 Core / Empty Core → 共享 Attribute / Empty Attribute
2. 新 Attribute 发现（最多 5 个）→ 结构编译 → 批量语义准入门禁
3. 名称、含义、输出结构多路 Top 5 召回 → RRF 排名融合
4. LLM 对每个 Top 候选独立判断 same / related_distinct / unrelated
5. 确定候选身份 → 关系图连通分组 → 候选唯一去向规划 → 逐字段定义/编译 → 全局语义门禁
```

固定字段读取 `data/definitions/discovery/core.yaml` 与
`data/definitions/discovery/attribute.yaml`。当前仓库中的 Discovery Core 是生产 Core 的独立
快照；Discovery Attribute 是受控子集，仅保留 `order_numbers`、`project_numbers`、
`delivery_locations`、`acceptance_mechanism` 和 `performance_security`，其余五个生产
Attribute 被有意留给发现流水线重新发现。两个目录必须与 production 的 `core.yaml`、
`attribute.yaml` 物理隔离；运行器会拒绝直接指定生产目录。架构仍支持 0 Core/0 Attribute，
需要验证纯冷启动时应另备显式 `status: empty`、`fields: []` 的 Discovery 快照，而不是改用
生产目录。

## 运行

```bash
python experiments/field_discovery_stage_one/run.py --input-dir data/input
```

### IDE 直接运行

直接运行 [`run.py`](run.py) 时，可在文件末尾的 `if __name__ == "__main__"` 编辑区修改
`IDE_INPUT_DIR`、`IDE_OUTPUT_DIR`、独立 Discovery Core/Attribute 目录、每份合同候选上限、
Top K、候选规则重试和组级收敛参数。若 IDE Run Configuration 或终端已经传入命令行参数，
则命令行参数优先，IDE 编辑区不会覆盖它们。

可用独立的非空 Discovery 目录验证固定字段提取复用：

```bash
python experiments/field_discovery_stage_one/run.py \
  --core-catalog data/definitions/discovery/core.yaml \
  --attribute-catalog data/definitions/discovery/attribute.yaml
```

需要本地 MLLM 与 Embedding 服务均已启动。Embedding 只作用于本批次新字段候选；固定
Discovery Core/Attribute 只作为模型 Prompt 和程序新颖性门禁的约束，不进入内存向量池。

候选生成专门采用跨合同缓存布局：不随合同变化的发现任务、冻结的固定字段定义和输出规则位于
PDF 图像前；图像后只追加本合同的页数说明与 Core/Attribute 状态。不同合同从图像开始分叉，
因此 vLLM 可复用更长的静态文本前缀。该实验性布局不改变正式 Core、Attribute、Clause 或
Abstract 的 Prompt 顺序。

候选生成采用“类型描述 → 程序编译”协议。模型只生成 `field_id`、`name`、`meaning`、
`output.type` 及该类型必需的递归描述、`extraction_rule`、证据和新颖性结论，不生成
`aliases`、`not_meaning`、`examples`，也不手写 JSON Schema。程序按类型补齐空值、对象必填键、
禁止额外属性和数组元素约束，再编译正式字段结构。类型描述的强 Schema 使用按 type 互斥的
`oneOf`，从生成阶段阻止 string 携带 items/values，并强制 array/object/enum 的必要参数。固定 Core/Attribute 以及候选比较均使用简洁
语义卡；只有已有且非空的别名或排除边界才展示，examples 和原始 JSON 不展示给模型。

候选批次的外层 JSON 必须可解析，且 `candidates` 最多五项；在此基础上，程序会逐项校验
每个候选。单项不符合 `CandidateProposal` 契约时，不影响同批已经合法的候选：程序保留合法项，
并针对该失败项发起一次不含 PDF 的定点结构修复。修复 Prompt 仅携带失败候选与程序校验错误，
要求保持同一业务事实和已有证据，不能借修复生成无关字段；修复仍失败时，仅该候选以
`proposal_schema` 原因被拒绝。若整个响应不是可解析 JSON 或外层包络不合法，无法可靠拆分
候选，仍按该合同的候选生成步骤失败处理。

字段归属判别是另一条轻量链路：候选已先通过 PDF 证据门禁，因此每个 Top 候选只与当前新字段
进行一次独立的纯文本定义比较，不发送 PDF 图像、页码或合同字段值。同一当前字段的比较按 Top
顺序执行，稳定的“任务规则 + 当前字段定义”位于前缀，待比较字段位于后缀；程序在收齐全部
比较后，才按 `same → related_distinct → unrelated` 的既定优先级决定身份。`same` 要求两个
顶层字段完整一一对应，只匹配 object 某个子字段会被程序拒绝并反馈重试一次；输出类型差异不能
单独支持 `unrelated`，相同 field_id 和规范名称也不能因“原文版/结构化版”差异被拆成
`related_distinct`。

候选结构编译通过后，同一合同最多五个候选会并发进行独立的纯文本语义准入。每次调用只携带
一个候选和固定字段定义；它按完整业务含义检查固定字段及其 object 子字段覆盖、字段原子性和规则/含义一致性，状态仅有 `accepted`、
`covered_by_fixed`、`non_atomic`、`invalid_rule`。后者只允许锁定身份、结构和证据后局部改写
规则一次，并重新执行位置与语义门禁；宽泛信息容器和固定字段衍生切片不会进入向量池。

每个候选的语义准入都允许针对 Schema 或固定字段引用错误重试一次。调用由请求限流器控制并发；
某个候选在两次尝试后仍无法通过语义门禁时，只记录该候选的 `semantic_gate_error` 并拒绝它，
不影响同一合同的其他候选继续进入候选池。

程序把全部非 `unrelated` 边维护为治理关系图；一个新候选连接多个既有组时会合并相应连通分量，
避免只选最高分锚点造成语义族碎片化。连通只用于提供完整治理上下文，不推断传递同义。

候选池冻结后，同一命令立即按关系图分量收敛。单候选分量由程序确定性直通；多候选分量先让
模型规划每个 candidate 的唯一去向，再锁定来源逐个生成最终字段定义。程序根据
`output.type` 编译正式结构和提取 JSON Schema，从来源名称确定性补齐 aliases，并在多字段分量
中补齐兄弟字段 not_meaning；examples 保持空。最后同时执行跨组 `field_id` 唯一门禁和全局
语义门禁。全局门禁由程序逐个绑定最终字段，模型每次只输出一个判断，并只比较紧凑身份/值边界，
检查固定覆盖、跨组重复与边界重叠；非 accepted 初判还要通过一次当前/目标字段对的聚焦复核，
以排除“业务相关即覆盖”的假阳性。`field_definition_drafts.json` 是第二阶段统计的
输入草案，不是正式 Attribute 目录。

`extraction_rule` 必须是跨合同规则，说明确认条件、排除边界、规范化以及缺失/冲突处理。页码、
条款号、固定章节标题和当前合同原句只属于 `evidence`，不得进入字段定义。候选门禁命中明确的
位置化表达后，会用仅允许重写字段及子字段规则的局部 Schema 把失败原因反馈给模型重试一次；
程序会剥离规则文本后逐项比对 `output`，模型不能借重试修改字段身份、值结构或证据。最终
组级定义会再次执行同一门禁。

## 阶段日志与输出

控制台和 `stage.log` 会输出 Prepare、五个业务节点、候选规则重试、Top 5 融合分数、逐对
LLM 三分类、候选池动作和最终组级收敛。日志不打印合同字段值、证据原文或模型 raw response。
`summary.json` 分别记录模型提出、门禁通过和门禁拒绝的候选数量，不能将三者混作“发现数量”。

模型输出采用“理由在前、决定在后”的固定顺序：候选对象以
`novelty_reason → status: accepted` 收尾，关系比较以 `reason → relation` 收尾；两种理由最长
1200 字符。关系比较的 `reason` 必须以 `因此 relation=<relation 字段值>` 精确收尾（末尾不再加
标点），程序会补齐遗漏的固定结尾，并拒绝已显式写出且与 `relation` 相反的结论。模型的
`status: accepted` 只表示建议进入门禁，程序仍可将其记录为
`rejected_by_gate`，不会让提示词绕过结构、新颖性或证据校验。

RRF 融合分数不是 0~1 概率。每个 Top 匹配还保存三个视角的原始相似度和名次，供人工标注后
校准阈值；当前不会用未经标定的硬阈值跳过 Top 5。manifest 尽力记录运行前后的 vLLM 前缀/
多模态缓存指标；指标端点不可用只会标记 unavailable，不阻断实验。

每次运行产生：

```text
experiments/outputs/field_discovery_stage_one/<run-id>/
├── manifest.json
├── stage.log
├── summary.json
├── 01_document.json
├── ...
├── candidate_pool.json
├── candidate_relation_graph.json
├── group_refinements.json
├── global_semantic_gate.json
├── field_definition_drafts.json
├── refinement_plan.json
└── NN_failure_diagnostic.json  # 单份合同失败时存在
```

单份合同记录保存固定字段状态、候选定义、证据页码和证据哈希、Top 5 分数、关系判断、规则
门禁尝试和身份/分组动作；不保存候选涉及的合同具体值或模型原始输出。批次中一份合同失败不会
阻止后续合同继续处理，但会令整个 manifest 的最终状态失败。人工分析时，必须按
`experiments/experiment-analysis-template.md` 在对应运行目录追加 `analysis.md`。

## 当前边界

- 不更新 Discovery 或 Production YAML；
- 不执行第二阶段全合同集回扫与命中率统计；
- 不写 Elasticsearch 或正式合同元数据；
- 这是实验实现，正式 `FieldDiscoveryService` 和批次用例仍待迁移到 `src/`。
