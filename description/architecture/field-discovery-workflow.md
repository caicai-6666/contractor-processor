# 字段发现两阶段工作流

> 状态：两阶段已迁入正式应用。第一阶段覆盖固定字段提取、候选级准入、三视角向量召回、
> Top 5 逐对三分类、稳定身份、关系图分组、并发组级收敛和全局语义门禁；第二阶段只接收
> 收敛后的冻结字段，按“单合同、单字段”并发回扫并确定性统计频率。历史实验入口反向复用
> `src/contract_processor/infrastructure/field_discovery/`，不再维护另一份算法实现。

---

## 快速导航

- [核心边界](#2-核心边界)：明确 discovery 的输入、产物与只读约束。
- [第一大步](#5-第一大步字段发现统一流水线)：候选池、Top 5 判别、关系图与组级收敛。
- [第二阶段](#6-第二阶段全合同集命中统计)：回扫、统计与专家审核前的验证。
- [对外接口](#8-对外接口与实现分层)：领域端口、实验实现和生产迁移边界。

---

## 1. 模块用途

字段发现模式用于从一批合同中建立和完善字段知识，而不是生成可直接入库的合同元数据。
它先确定“有哪些新字段、哪些候选是同一字段、不同字段应如何分组”，再回扫完整合同集统计
这些字段的实际命中效果，最终生成专家可审核的字段知识包。

该模式与 `production` 完全隔离：不执行 Clause 和 Abstract，不读取或修改生产 Core、Attribute
目录，也不把发现过程中临时提取的合同字段值写入 Elasticsearch。

> **产物定位：** Discovery 的最终产物是可供专家审核的字段知识包，而不是可直接入库的合同元数据。

---

## 2. 核心边界

字段发现包含三类对象，三者生命周期不同：

| 对象 | 职责 | 是否进入候选向量池 | 是否可在运行中修改 |
| --- | --- | --- | --- |
| Discovery Core | 已知的核心字段约束，并为发现模型提供合同上下文 | 否 | 否 |
| Discovery Attribute | 已知的扩展字段约束，并防止模型重复发现 | 否 | 否 |
| 新字段候选 | 本批次新发现的字段身份、关系和分组 | 是 | 仅更新批次内候选池 |

Discovery Core 和 Discovery Attribute 是批次启动时冻结的固定约束，不属于新字段候选，也不
参与候选池分组。只有通过结构和新颖性门禁的新字段才生成向量，并在本批次的新候选之间进行
相似度召回。

> **池化边界：** 固定目录用于定义“已覆盖空间”；只有本批次的新候选才能进入内存向量池、参与关系判别和分组。

---

## 3. 独立字段目录

Discovery 必须使用与生产目录物理隔离的机器规范：

```text
data/definitions/
├─ core.yaml
├─ attribute.yaml
└─ discovery/
   ├─ core.yaml
   └─ attribute.yaml
```

已配置的目录项为：

```yaml
paths:
  core_fields: data/definitions/core.yaml
  attribute_fields: data/definitions/attribute.yaml
  discovery_core_fields: data/definitions/discovery/core.yaml
  discovery_attribute_fields: data/definitions/discovery/attribute.yaml
```

两个 Discovery 目录都允许为空，但必须使用显式空目录，不能省略 `fields` 键：

```yaml
schema_version: "0.1"
field_set: core
status: empty
fields: []
```

- Discovery Core 为空时，Core 节点确定性返回空结果，不调用模型；
- Discovery Attribute 为空时，固定 Attribute 提取步骤直接跳过；
- 一次批次只读取目录快照，处理中不覆盖 YAML；
- 专家审核通过后，由独立治理动作生成新的 Discovery 或 Production 目录版本。

当前仓库的受控实验快照不是空目录：Discovery Core 复制全部生产 Core；Discovery Attribute
只保留 `order_numbers`、`project_numbers`、`delivery_locations`、
`acceptance_mechanism` 和 `performance_security`。其余五个生产 Attribute 被刻意排除，以
验证新字段发现、关联和组级归并能力。该选择是实验数据配置，不改变上述 0 Core/0 Attribute
运行能力，也不修改生产目录。

Discovery 与 Production 可以复用同一套字段定义契约、目录加载器和逐字段提取能力，但必须
通过不同配置路径获得目录快照，禁止根据运行模式偷偷改写生产文件。

> **目录快照：** 空 Discovery 目录是合法的冷启动配置；目录一旦被本批次读取即冻结，只有独立的专家治理动作可以生成新版本。

---

## 4. 宏观流程

整个批处理分为两个大阶段：

```text
第一大步：发现并收敛字段定义（一次运行）
  1. 提取 Discovery Core → Discovery Attribute
  2. 发现新 Attribute → 结构编译 → 单候选并发语义准入门禁
  3. 名称 / 含义 / 输出结构多路召回 → RRF 融合 Top 5
  4. LLM 对 Top 5 逐对判别 same / related_distinct / unrelated
  5. 确定身份 → 关系图连通分组 → 冻结候选池
     → 候选唯一去向规划 → 逐最终字段定义/编译 → 全局语义门禁

第二阶段：统计字段效果
  冻结字段池 × 完整合同集
    → 逐字段验证是否命中
    → 汇总不同合同数和状态分布
    → 生成专家审核包
```

第一阶段解决字段知识身份问题；第二阶段只在身份稳定后评估字段效果。新字段无论在批次的
第几份合同中首次出现，都必须在第二阶段回扫完整合同集，避免处理顺序造成命中率偏差。

> **先身份，后统计：** 第一阶段只回答“字段是什么、彼此有什么关系”；第二阶段才在冻结字段池上衡量跨合同命中效果。

---

## 5. 第一大步：字段发现统一流水线

> **职责分离：** 模型提出候选、解释字段关系或生成单字段定义；程序编译结构、执行门禁、决定身份和分组，并拒绝不合规结果。

### 5.1 步骤一：提取固定 Core 和 Attribute

Core 使用 `discovery_core_fields` 指向的冻结目录，逻辑上复用正式 Core 的逐字段提取实现。
结果只在本次 Discovery 批次中使用。

传递给后续模型的 Core 上下文必须保留所有字段状态，但使用简洁表达，不传递完整审计包络：

```text
- 合同名称：设备采购合同
- 合同编号：未找到
- 合同文书角色：main_contract
- 合同金额：110000 CNY
- 签订日期：存在歧义
```

`not_found`、`not_applicable`、`ambiguous`、`conflicting` 和技术失败不能统一压缩为 `null`，
因为它们表示不同的覆盖和质量状态。保留空字段可以明确告诉发现模型“字段已经定义，只是
当前合同未提取到”，避免把固定 Core 的同义概念重新创建为 Attribute。

#### 固定 Attribute

Attribute 使用批次启动时冻结的 `discovery_attribute_fields` 目录，并在 Core 完成后逐字段
提取。它同样是固定约束，不属于候选池。

第一阶段不能随着候选池增长而对每份后续合同提取所有新候选，否则会产生处理顺序偏差和
不断增长的模型调用量。当前批次新发现的字段统一留到第二阶段回扫完整合同集。

### 5.2 步骤二：发现新 Attribute

字段发现模型必须直接读取原始 PDF，输入至少包括：

- 原始合同页面；
- 固定 Discovery Core 和 Attribute 定义；
- Core 与固定 Attribute 的简洁提取结果，包括未命中状态；
- 字段定义结构规则；
- 新字段必须具有稳定业务含义、跨合同复用价值和原始证据的约束。

候选必须是合同正文明确陈述或约定的业务事实。合同主要书写语言、PDF 页数、文件格式、
扫描/OCR 质量、字体版式或签章颜色等从文档载体本身推知的属性不属于 Attribute discovery；
它们应由确定性预处理或 Core 元数据管理。合同正文明确约定的适用语言或效力语言仍是业务事实，
不受该排除规则影响。

每份合同最多提出 5 个新字段，但允许返回少于 5 个或 0 个。该数量是候选预算，不是必须
凑满的目标。规划配置如下：

```yaml
field_discovery:
  max_candidates_per_document: 5
```

候选生成模型输出的是字段身份提议，而不是完整正式定义。提议至少包含 `field_id`、`name`、
`meaning`、基于 `output.type` 的递归类型描述、`extraction_rule`、当前合同证据和新颖性说明。
第一步不让模型生成或阅读空的 `aliases`、`not_meaning`、`examples`；这些信息需要多个候选
形成依据后在组级收敛与专家治理中补充。固定字段已有的非空别名和排除边界仍以简洁语义卡展示，
用于准确表达已覆盖范围；字段示例和原始 JSON 不进入发现 Prompt。

#### 模型与程序的定义职责

模型只负责描述值类型：标量选择 `type`，enum 给出值及业务含义，object 逐项描述子字段，
array 描述单个元素。程序按 `output.type` 分发编译，统一生成 `nullable`、object 的完整
`required`、`additional_properties=false` 及数组元素非空约束，并为每个最终字段独立生成提取
JSON Schema。提供给模型的类型描述 Schema 本身按 `type` 使用互斥 `oneOf`：string 不可能携带
items/values，array 必须携带 items，object 必须携带 properties，enum 必须携带 values。模型输出
`nullable`、`required`、`additional_properties`、`anyOf`、跨类型参数或抽取结果包络键都会被
严格 Schema 或程序门禁拒绝。

`extraction_rule` 与本合同 `evidence` 必须严格分离：`evidence.page_number/source_text` 记录
当前合同在哪里提供了证据；`extraction_rule` 只描述跨合同适用的确认条件、排除边界、
规范化方式以及缺失或多候选冲突处理。规则不得出现页码、条款号、章节序号、固定章节标题、
版式位置、当前合同主体、具体值或原句。规则应使用“与当前字段所描述的合同事项直接关联”这类
中性表达，并根据字段自身含义具体化；不得复制“开票义务”等其他业务领域示例，也不能使用
“从条款7其他约定中提取”等位置锚点。正则必须写入 `output.pattern`，不能伪装成
`format: "pattern: ..."`；只有表达真实字符格式限制时才能提供 pattern，`.*`、`^.*$`、`.+`、
`^.+$` 等无约束正则会被程序拒绝。string pattern 也不能用多个自然语言业务短语分支伪装枚举；
封闭集合必须使用 enum，开放文本必须省略 pattern。

#### 步骤二内部门禁

模型提议不能直接进入向量池。程序先执行：

1. `field_id`、必填键和禁止额外键校验；
2. 按 `output.type` 校验递归类型描述，并编译为正式 `output`；
3. 字段名称不得包含某份合同的具体值；
4. 证据位置和来源合同身份校验；
5. 与固定 Core/Attribute 的 `field_id`、规范化名称和别名精确冲突校验；
6. `extraction_rule` 不得包含当前合同位置证据；命中时仅允许模型局部修订规则一次；
7. 已通过 JSON Schema、但未通过递归 output、领域定义或固定字段精确冲突等程序契约的候选，
   也只对该候选局部重试一次。重试锁定 `field_id`、`name`、`meaning`、`evidence`、
   `novelty_reason` 和 `status`，仅允许修正 `output` 与 `extraction_rule`；修复后必须重新执行
   全部程序门禁，因此不能借重试绕过固定字段覆盖或证据约束；
   定义自身也不得冲突，例如付款安排不能把 output 中作为合法阶段的预付款又在规则中整体
   排除；此类确定矛盾同样只重试当前候选一次；
   enum 的 extraction_rule 明示的每个字面类别也必须能由 `output.values` 表示，否则模型会在
   回扫时被迫把未知类别映射成错误枚举值；程序要求补全集合或改用开放 string；
8. 对同一合同已通过结构门禁的每个候选并发执行独立的纯文本语义准入，状态仅允许
   `accepted`、`covered_by_fixed`、`non_atomic`、`not_attribute`、`invalid_rule`；
9. 语义门禁按完整业务含义检查固定字段及其 object 子字段覆盖，拒绝把多个可独立缺失、检索或
   治理的事项打包为宽泛 object，并检查规则是否混入其他业务领域。`non_atomic` 必须明确指出
   至少两个实际包含在当前候选定义内的独立业务问题，不能拿候选外的相邻字段证明其不原子；
   同一事实的多值、重复项、数组表示或不可分割的结构化组成不构成 `non_atomic`。字段来自合同
   条款、同时保留 Clause 原文或依赖其他合同数值理解，也不单独构成拒绝理由。`not_attribute`
   专门拒绝非合同明示业务事实的文档载体属性。普通合同序言中的“经协商达成如下协议、共同
   遵守/恪守”不能作为正式签署前另有初步协议的证据；同时包含付款触发事件、付款期限和逾期
   后果的宽泛“付款条件”会合并多个可独立缺失的问题，必须拒绝；
10. `invalid_rule` 只允许锁定字段身份、output 结构和证据后局部修订一次，随后必须重新通过位置
   与语义门禁，不能借重试改变候选。

固定 Core/Attribute 通过 Prompt 和上述覆盖门禁约束新字段，不进入候选向量索引。每个候选的
语义准入单独调用、由请求限流器控制并发；某项在一次纠错后仍失败时，只拒绝该候选，不影响
同合同其他候选。结构不合法或已经被固定字段覆盖的提议在此处终止，并记录拒绝原因。
当 `covered_by_fixed` 遗漏目标 ID、但理由中唯一明确写出一个固定字段 ID、名称或别名时，程序
只恢复该唯一引用并记录恢复结果；理由没有唯一命中时仍按非法响应纠错，不作猜测。

> **准入结论：** 未通过结构、证据或语义门禁的提议不得进入向量召回；局部重试只能修复规则，不能借机改写候选身份、结构或证据。

#### 正式第一阶段实现

正式实现位于 `src/contract_processor/infrastructure/field_discovery/service.py`，由
`StructuredFieldDiscoveryService` 提供。它读取原始 PDF 页面、冻结的 Discovery Core／Attribute
定义及其提取结果，以强 JSON Schema 生成最多五个候选；随后对每个候选分别发起纯文本语义准入。
这些门禁请求可并发调度，但统一经过 `ModelRequestLimiter`，实际并发上限始终等于
`models.mllm.max_concurrent_requests`。任意一个候选失败只会拒绝该候选，不会丢弃同批已经通过的
候选。第一阶段提议不生成 `aliases`、`not_meaning` 或 `examples`；语义门禁检查固定字段覆盖、
原子性和规则一致性，治理字段只在组级收敛后按来源候选和兄弟字段边界确定性生成。

候选生成、单候选语义准入、候选修复、规则修订、逐对关系判断、组级规划、最终字段定义和
全局门禁的稳定任务规则统一维护在 `infrastructure/extraction/discovery/prompts/`；运行时只注入
目录、候选、字段组、校验反馈和预算等动态上下文。该目录全部正式任务文件均纳入
`prompt_version`，其中第二阶段字段提取规则为 `02_extract_candidate_field.txt`。

即使模型网关返回的候选数组整体未能通过本地 Pydantic 校验，服务也会逐项解析：已合法的候选
立即保留，只有非法项会单独重试一次；再次失败的项才被拒绝。候选虽然通过 Pydantic、但在后续
程序契约中出现 `format: "pattern: ..."` 伪装正则、递归 output 不完整等错误时，同样只重试该
候选一次，并逐项锁定其业务身份与证据，只允许修复 `output` 和 `extraction_rule`。该恢复逻辑
不会重跑或覆盖同批合法候选，也不能把失败候选改造成另一个业务字段。

`DiscoverFieldsFromBatch` 是批次父图。它按合同顺序调用共享的
`StructuredFieldDiscoveryService`，因此所有合同复用同一内存向量池；全部合同完成后只调用一次
`consolidate()`，生成候选池报告、关系图、组级报告、全局门禁和 `frozen_candidates`。
`frozen_candidates` 此时已是收敛并通过全局门禁的最终字段草案，第二阶段不会接收原始候选。
同字节 PDF 副本按 `document_id` 去重，避免重复参与身份和频率统计。

单合同构建器拥有独立 discovery 服务和 Embedding 客户端，执行结束时连同页面/MLLM 资源一起
关闭；批次构建器不在每份合同后关闭共享服务，而是在组级收敛完成后统一关闭，确保候选池完整。

正式 CLI 可用 `--max-documents N` 做受控回归，只截取排序后的前 N 份 PDF，绝不修改输入目录。

### 5.3 步骤三：多路召回并融合 Top 5

通过门禁的新字段生成查询向量，并只检索当前批次已经获得独立身份的新候选。候选池初始为
空；第一个合格新字段没有可比较对象，因此直接创建身份和分组，并将其向量加入内存索引。

向量采用多视角正向语义：

| 视角 | 内容 |
| --- | --- |
| 名称视角 | `name`（第一步尚无经治理的别名） |
| 含义视角 | `meaning` |
| 结构视角 | `output` 的类型和结构摘要 |

`not_meaning`、反例以及提取规则中的否定表达不进入正向向量。Embedding 对否定关系的表达
不够稳定，把“订单编号不是合同编号”整体向量化可能反而提高两者相似度。这些排除边界只在
后续 LLM 精判时使用。

各视角分别召回后使用排名融合并去重，形成融合相似度降序的 Top 5。第一版不使用未经真实
标注校准的硬阈值直接合并字段；不足 5 个时比较全部已有候选。

> **召回不是决策：** RRF 分数只用于构造比较集合，不是同义概率，也不能直接决定字段合并。

### 5.4 步骤四：LLM 判断完整 Top 5

向量只负责构造局部比较集合。LLM 必须判断完整 Top 5，不能在第一个结果后提前停止。每一项
只允许以下三个关系：

| 关系 | 含义 |
| --- | --- |
| `same` | 两个顶层字段完整一一对应同一个业务事实 |
| `related_distinct` | 两者语义相关，但必须保留为不同字段 |
| `unrelated` | 两者不存在需要保留的字段关系 |

每个 Top 候选均以独立的一次纯文本调用进行判断：输入仅包含任务规则、当前新字段定义和一个
待比较字段定义，以 `meaning`、`extraction_rule` 和递归 `output` 为主要边界；若已有非空
`aliases` 或 `not_meaning` 才按需展示。不传 PDF 图像、页码、
合同字段值或来源证据；候选是否有原始合同依据已由前置证据门禁负责，字段归属只需要比较定义
边界。只与 object 某一个子字段相同不能判 `same`；输出类型不同只能触发继续比较，不能单独
支持 `unrelated`。两个候选具有相同 field_id 和规范名称时，程序也拒绝仅因原文版/结构化版差异
判成 `related_distinct`。字段对请求独立并发，结果仍按 Top 顺序归档；全部比较必须完成，不能在出现第一个 `same` 后提前停止。程序收齐比较结果后
才执行身份或分组决策。`reason` 在解释边界后必须固定以
`因此 relation=<relation 字段值>` 收尾；程序会规范化遗漏的结尾，并拒绝已显式写出但与结构化
`relation` 相反的结论。Schema 解析失败或顶层/子字段危险错配会把清晰失败原因反馈给同一字段对
重试一次；失败记录只保存错误类型、简短原因、finish reason 和 token 指标，不保存原始响应。
LLM 不直接创建身份、选择分组或修改候选池。

> **模型决策边界：** LLM 只对每一对字段输出 `same`、`related_distinct` 或 `unrelated`；程序必须收齐完整 Top 5 结果后才执行后续决策。

### 5.5 步骤五：关系图分组与两阶段收敛

程序在 Top 5 全部完成判别后，按以下优先级执行：

```text
至少存在一个 same
  → 复用融合相似度最高的 same 目标身份
  → 丢弃新 candidate_id，不创建新身份

没有 same，但存在 related_distinct
  → 创建新字段身份
  → 与全部 related_distinct 目标建立治理关系边

全部都是 unrelated
  → 创建新字段身份
  → 创建新的独立分组
```

`same` 的优先级高于向量排名：即使排名第一的字段是 `related_distinct`，只要 Top 5 中存在
一个 `same`，就必须复用该相同字段的身份。多个目标同时被判定为 `same` 时，选择融合相似度
最高者作为身份；其余非 `unrelated` 目标仍进入关系图，供组级治理看见潜在重复或相邻字段。

“丢弃新身份”不等于丢弃来源。本次合同、证据位置和判定理由仍关联到被复用的身份；同一
合同对同一字段只能计为一个不同合同命中。

#### 关系图与治理分量

`related_distinct` 只表示“应放在同一治理上下文中”，不表示字段等价。程序把全部 `same` 和
`related_distinct` 判定保存为关系边，并以无向连通分量生成唯一 `group_id`。如果一个新候选分别
连接了原有两个组，这两个组会合成同一治理分量，从而避免付款、争议等同一语义族因“只选最高分
锚点”被永久拆散。连通具有传递性只用于组织治理上下文，绝不能据此推断 A 与 C 是 `same`，
字段身份仍由逐对关系和后续组级模型决定。

#### 组级收敛与全局门禁

全部合同处理完毕后冻结候选池。单候选分量不存在组内归并关系，由程序直接复用定义并编译
Schema，不调用模型。多候选分量拆成两次职责单一的调用：

1. **候选唯一去向规划**：模型只输出 `field_plan_01...`，为每个 plan 给出来源 candidate、
   字段名称/含义/边界，或明确淘汰；每个 candidate 必须且只能出现一次，不生成复杂 output。
   如果窄候选只是结构化字段子字段加筛选条件得到的查询切片（如“预付款比例”来自“付款阶段
   [].付款比例”），程序要求淘汰该切片，不得建立兄弟字段或并入 aliases；
2. **单字段定义生成**：程序锁定一个 plan 的来源后，分别让模型生成一个字段的
   `field_id/name/meaning/output.type/extraction_rule`，再按 type 编译正式 output 与动态提取
   JSON Schema；模型不生成 aliases、not_meaning 或 examples。

来源候选名称和既有别名会被程序确定性补入 aliases；一个分量保留多个字段时，各兄弟字段名称
会补入彼此 `not_meaning`，确保边界非空。`examples` 仍为空，必须等待第二阶段真实提取和专家
治理，不能编造。Schema 解析、候选覆盖、规则泛化或字段契约错误都进入同一个最多一次的反馈
重试，并保留脱敏指标。

兄弟字段及身份计划中的 boundary 是最终定义的硬边界，不能一边把兄弟字段写入 `not_meaning`，
一边又在 meaning 或 extraction_rule 中把它作为当前字段的模式、正例或兜底值。当前程序还对可
确定的付款语义矛盾执行本地校验：若“付款方式”已经排除“分期付款安排”，就不能再把“分期支付”
列为可采纳方式；“付款安排”也不能整体排除预付款或首付款。

单字段定义会递归校验 output、规则泛化性和 `format`/`pattern` 一致性，无约束 pattern 与非法
正则不能进入最终字段。若模型在反馈重试后
仍只留下语法非法的可选 `pattern`，程序可移除该不可执行约束并重新执行完整字段契约，同时在
组级审计中记录恢复路径；字段身份、类型和其他约束不能借此改变。一个分组中的最终字段分别
并发定义，某个 plan 最终失败时保留同组其他已合法字段并把该组标记为部分成功，而不是整组清空。

所有组完成后再执行全局语义门禁。程序逐个绑定当前最终字段，模型每次只输出一个判断，同时检查：是否被固定 Core/Attribute（含对象
子字段）覆盖、是否与其他组字段重复、是否存在不能安全自动合并的边界重叠。冲突只标记
`covered_by_fixed`、`duplicate_final` 或 `overlap_review` 并令批次不可推广，不会由程序静默删除。
任何非 `accepted` 初判还要接受一次只展示当前字段与目标字段的聚焦复核；同段共现、触发依赖、
共享上位主题或复用同一原文必须被复核为假阳性，只有目标能提供同一个规范值或边界确实无法
分离时才保留冲突。
跨组 `field_id` 唯一门禁仍保留，但不能替代该语义门禁。

> **冲突处理：** 全局门禁只标记 `covered_by_fixed`、`duplicate_final` 或 `overlap_review` 并阻止批次推广；程序不得静默删除或自动合并冲突字段。

正式流程通过 `run_batch --mode discovery` 一次执行两个阶段，并以标准输出返回候选池、关系图、
最终字段草案、逐合同观察和频率统计。实验 `field_discovery_stage_one/run.py` 及独立
[组内收敛入口](../experiments/field-discovery-group-consolidation.md)仅用于历史产物复现和 Prompt
调试，不再维护独立算法或作为正式流程的必经命令。

批次结果顶层生成唯一 `batch_id`、`started_at`、`completed_at` 和 `processing`。后者冻结 MLLM
模型、全部正式 Prompt 内容哈希、Embedding 模型/维度/字段摘要指令哈希、Discovery Core／
Attribute Schema 版本与目录模式，以及候选预算和 Top-K；统计因而不会成为脱离运行版本的裸数字。

#### 增量候选池

每处理完一份合同，获得新身份的候选立即加入内存向量池，供下一份合同召回：

```text
合同 1 → 候选池 V1
合同 2 + 候选池 V1 → 候选池 V2
合同 3 + 候选池 V2 → 候选池 V3
...
```

固定 Discovery Core/Attribute 始终在候选池之外。一次运行结束后冻结最终候选身份和分组，
内存向量随进程对象释放，不写入 Elasticsearch。

---

## 6. 第二阶段：全合同集命中统计

第一阶段结束后，冻结新字段身份及定义，再让每个新字段回扫完整合同集。模型按单字段动态
Schema 临时验证字段是否存在；字段值可以为完成结构和语义校验在内存中短暂产生，但不得写入
正式合同元数据或 Elasticsearch。

> **统计边界：** 正式字段目录与批次统计分离；待审核 YAML 可将统计挂在候选定义下。技术错误必须单独计数，不能伪装成 `not_found`；同一合同的重复表述也不能抬高不同合同命中数。

### 6.1 正式子图与并发粒度

正式第二阶段子图只包含两个业务节点：

```text
START
  → extract_candidate_field（LangGraph Send 动态并发）
  → calculate_candidate_statistics（确定性 Python 聚合）
  → END
```

父图把冻结候选与不同 `document_id` 的合同集合做笛卡尔积；每个 `Send` 只携带一份合同和
一个候选字段。每次 MLLM 请求只包含当前 PDF、一个冻结字段定义和对应的单字段强 JSON Schema，
禁止把多个候选合并为一次数组生成。单项结构或业务校验失败后只重试当前字段一次，再失败则生成
`task_status=failed` 的观察，不影响其他字段—合同任务。

`CandidateFieldExtractionService` 在批次内缓存每份合同的全页渲染结果，避免同合同的不同字段重复
执行 PyMuPDF 渲染；模型请求仍各自发送完整页面。所有动态任务共享同一个
`ModelRequestLimiter`，LangGraph 中的待执行任务数可以大于模型实际并发数，实际并发上限由
`models.mllm.max_concurrent_requests` 决定。

冻结字段中的动态 `pattern` 会保留在字段 Prompt 和正式定义中，但不直接交给 vLLM 的 xgrammar
生成语法；任意模型生成正则可能使后端在字符串闭合前错误结束。模型先按不含动态 pattern 的
结构 Schema 生成完整 JSON，程序解析后再用 Draft 2020-12 对 `found` 规范值执行同一递归正则
约束。失败原因会反馈具体字段路径和约束后只重试当前任务一次，最终契约没有被放松。

回扫还会执行三类可确定的业务校验：`output.unit=percent` 时，程序从最小原文识别 `%`、`‰`、
`‱` 及“百分之/千分之/万分之”比例，并核对规范值使用百分数口径，例如 `40%=40`、`1‰=0.1`、
`万分之一=0.01`；交付/交货/运输方式字段必须有快递、物流、送货、自提等具体机制，只有“发货”
或“交付”动作及其时间条件不能计为 found；付款方式必须有转账、汇款、现金、信用证、款到发货
等独立支付工具或结算机制，多阶段比例、金额和期限的整段付款计划不能重复计入，且原文不得混入
合同回传、作废、取消等相邻事实；发票类型必须保持专票/普票类别一致，发票税率原文必须保留
发票、开票或增值税语境，发票备注必须直接绑定票面需注明内容，不能吸收含税价、运费或物流等
相邻约定。任一错误只触发当前合同—字段任务局部重试一次。

对象字段的通用外层状态由子字段确定性汇总，但字段自身可以施加更严格的完整性规则。例如
`penalty_for_late_delivery` 只有责任方、没有违约金比例、计费基数和计罚方式时，只能证明一般
逾期责任，不能判为违约金 `found`；程序会把“唯一命中责任方、三个计算要素均未命中”的窄场景
安全降级为整体 `not_found`。其他部分要素缺失或责任方原文与规范值矛盾时仍进入单字段反馈门禁，
不得用通用降级掩盖不确定结果。

字节完全相同的输入副本具有相同 `document_id`，第二阶段只计为一份不同合同。第一阶段若
Core 或候选发现等硬门禁失败，该文件仍参与第二阶段回扫：候选可能来自批次内其他合同，不能因
其自身第一阶段失败而制造统计盲区。固定 Attribute 的单字段失败不会再使整份合同退出第一阶段；
成功字段和该合同生成的合法候选会继续参与收敛，失败字段另记为局部诊断。

### 6.2 观察与频率口径

固定 Core 和固定 Attribute 已在第一阶段按各自门禁提取；固定 Attribute 可能缺少重试后仍失败的
字段，因此其技术失败必须单独审阅，不能按 `not_found` 统计。新候选必须执行第二遍全量验证，
不能只统计首次提出该字段之后处理的合同。

字段统计在批次审核 YAML 中挂在对应字段定义的 `statistics` 键下，至少记录：

```yaml
fields:
  - field_id: confidentiality_period
    name: 保密期限
    meaning: 合同明确约定的保密义务持续期间或终止条件。
    aliases: []
    not_meaning: []
    output: {type: string, nullable: true}
    extraction_rule: 仅提取与保密义务直接绑定的持续期间或终止条件。
    examples: []
    statistics:
      candidate_ref: group_0001:confidentiality_period
      document_count: 100
      scanned_document_count: 99
      found_document_count: 23
      not_found_document_count: 65
      not_applicable_document_count: 8
      ambiguous_document_count: 2
      conflicting_document_count: 1
      failed_document_count: 1
      frequency: 0.232323
      conservative_frequency: 0.23
      found_source_names: [采购合同A.pdf, 设备合同B.pdf]
      failed_source_names: [服务合同C.pdf]
```

命中频率不能写入正式 `attribute.yaml`，也不能成为脱离批次和分母的字段固有属性。批次审核
YAML 顶层必须绑定批次、字段目录版本、模型版本和 Prompt 版本；每项统计嵌入对应候选定义，
便于人工审核时同时观察“字段是什么”和“在本批合同中出现多少”。技术错误单独计数，不伪装成
`not_found`；同一合同内多次出现也只增加观察次数，不重复增加不同合同命中数。

正式 DTO 使用 `frequency = found_document_count / scanned_document_count`；另提供
`conservative_frequency = found_document_count / document_count`。前者用于评价已成功扫描合同中的
命中表现，后者把技术失败保留在总分母中，便于观察最保守下界。`ambiguous`、`conflicting`、
`not_applicable` 均属于已成功扫描但非命中状态；`failed` 不得并入 `not_found`。
运行 DTO 内部使用 `document_id` 保证相同字节合同去重稳定；最终审核 YAML 不展示这些哈希，
而使用 `found_source_names` 和 `failed_source_names` 列出原始 PDF 文件名，方便人工定位。

---

## 7. 输出与专家审核

Discovery 输出的是字段知识审核包，而不是合同入库包。审核包至少包含：

- 固定 Discovery Core/Attribute 目录版本；
- 新候选的稳定身份、完整建议定义、关系边和治理分量；
- `same` 映射、`related_distinct` 关联和疑似重复冲突；
- 来源 `document_id`、页码或坐标等最小证据定位；
- 字段命中统计及各状态计数；
- 模型、Prompt、Embedding 和批次版本；
- 专家审核状态与决定理由。

运行响应保留每个字段—合同任务的内存观察值、原文和状态，供统计审计；这些观察不写入正式合同
存储或 Elasticsearch，也不等同于生产抽取结果。专家可以决定把候选纳入新的 Discovery Core、
Discovery Attribute、Production Core 或 Production Attribute 版本，也可以合并、拒绝或继续
观察。任何模型判断都不能直接修改目录。

> **审核边界：** 只有专家的目录决策能够把候选写入新的 Discovery 或 Production 字段目录；模型输出始终只是审核材料。

正式批次完成后，`YamlFieldDiscoveryResultStore` 将通过全局门禁的冻结字段与第二阶段统计一一
关联，并原子写入 `data/definitions/discovery/result/<batch_id>.yaml`。当前输出
`schema_version: "0.2"`，文件采用 `status: draft`；批次信息会分别记录第一阶段整份失败合同和
Attribute 局部失败合同。`fields[]` 中每个字段项保持 Attribute 字段定义结构，只增加
`statistics`，其中也保存
`candidate_ref`、`group_id` 和来源候选 ID。不同批次使用独立文件，不覆盖历史批次；写盘或关联
校验失败会使当前 discovery 调用失败，不能静默返回缺少审核产物的成功结果。

---

## 8. 对外接口与实现分层

正式批次用例职责为：

```text
DiscoverFieldsFromBatch
  → 冻结 Discovery 字段目录和合同集
  → 逐合同调用已有 Prepare/Core/Attribute 能力
  → 调用 FieldDiscoveryService 生成新候选
  → 调用 FieldSimilaritySearcher 查询新候选池
  → 调用字段关系判别器执行三分类
  → 应用确定性的身份与分组策略
  → 回扫合同集并输出统计审核包
```

- `application` 保存批次用例、LangGraph 状态、结果 DTO 和确定性统计；
- `infrastructure/field_discovery` 保存候选向量池、模型结构契约、身份/分组规则和两阶段抽取服务；
- `infrastructure/persistence` 原子保存按批次命名的待审核字段定义与统计 YAML；
- `infrastructure/llm` 提供共享请求限流等通用模型能力；
- LangGraph 负责节点依赖和状态传递，不决定字段身份；
- `experiments/` 只保留历史复现入口，并反向复用正式算法；
- 正式批次只落盘待审核 discovery YAML，不写正式字段目录、合同结果或 Elasticsearch。

> **分层边界：** 检索端口只负责召回，LLM 端口只负责结构化调用，LangGraph 只负责依赖和状态；字段身份与治理规则属于 `application`。

---

## 9. 依赖与注意事项

- 候选内存索引使用 `llama-index-core` 的 `SimpleVectorStore`，不使用 Elasticsearch；
- 所有候选视角必须使用相同 Embedding 模型、指令、维度和归一化策略；
- 向量相似度只用于 Top 5 召回，不能直接决定 `same`；
- 固定字段约束与候选向量池必须使用不同对象和端口语义，防止固定目录被错误分组；
- 第一阶段模型调用失败不能创建候选身份；第二阶段失败不能计为字段未命中；
- 批次合同集合、目录快照和模型版本必须冻结，确保统计可复现；
- 正式 `CandidateVectorPool` 只接收本批次已准入的新候选；固定 Core/Attribute 仅作为覆盖约束，
  不能传入向量池。
