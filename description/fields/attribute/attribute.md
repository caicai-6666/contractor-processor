# Attribute 字段设计

> 当前实现状态：双模式拓扑、0 Core 策略、字段发现端口和固定 Attribute 提取服务均已实现；
> 正式 Attribute 使用 `0.3/draft` 初始目录逐字段提取。`draft` 表示字段语义仍待代表性合同
> 集审核，不表示生产运行可跳过或静默忽略该目录。
>
> 规范地位：[`data/definitions/attribute.yaml`](../../../data/definitions/attribute.yaml) 是正式
> Attribute Definition 的唯一机器规范源。
>
> 架构依据：两种运行方式、服务隔离和迁移顺序见
> [Attribute 双运行模式设计](../../architecture/attribute-operating-modes.md)。

---

## 快速导航

- [双重业务角色](#1-attribute-的双重业务角色)：Candidate 与正式提取结果的区别。
- [字段发现模式](#2-字段发现模式)：候选生成、事实来源与治理边界。
- [正式生产模式](#4-正式生产模式)：固定目录、输出与失败语义。
- [专家治理](#5-专家治理与目录变更)：审核、版本与目录调整规则。

---

## 1. Attribute 的双重业务角色

Attribute 在项目中不是始终“动态输出任意键”的字段容器。它在两个显式运行模式中承担
不同职责：

| 运行模式 | Attribute 职责 | 输出 |
| --- | --- | --- |
| `discovery` | 阅读合同原文，发现 Core 和已有 Attribute 未覆盖的字段概念 | 待治理的 Attribute Candidate |
| `production` | 严格按照正式 Attribute 目录提取固定字段值 | Attribute Extraction |

Attribute Candidate 是字段提案，不是正式字段。Attribute Definition 是经专家确认、带版本
且可以进入生产提取的扩展字段。二者必须使用不同 Schema 和生命周期，不能因名称相同而
混为一个对象。

Core 与正式 Attribute 的长期边界为：

- Core 保存跨合同普适、业务关键、适合作为主要过滤条件的稳定字段；
- Attribute 保存已被确认但通常只适用于特定合同类型、业务领域或组织的稳定扩展字段；
- 尚未审核的发现结果只存在于 Candidate 审核队列，不得直接进入任一正式目录。

> **对象边界：** Candidate 是字段知识提案；Attribute Extraction 是合同的固定字段结果。两者使用不同 Schema、生命周期与持久化边界，不能互相替代。

---

## 2. 字段发现模式

字段发现模式读取与生产目录隔离的 Discovery Core 和 Discovery Attribute，并把它们作为
固定的“已覆盖字段空间”。两个目录均允许为空，因此同一模式也支持项目从零字段开始冷启动。
固定字段通过 Prompt 与新颖性门禁约束候选，不进入新字段候选向量池。

> **发现边界：** 固定 Discovery 目录定义“已覆盖空间”，但候选模型仍必须直接阅读原始 PDF；Core 与 Attribute 结果只能作为辅助上下文。

当前受控重发现实验使用非空的
[`data/definitions/discovery/attribute.yaml`](../../../data/definitions/discovery/attribute.yaml)。该快照
从生产目录复制并保留 `order_numbers`、`project_numbers`、`delivery_locations`、
`acceptance_mechanism` 和 `performance_security` 五个固定字段；有意不纳入
`delivery_commitment`、`payment_schedule`、`invoice_requirement`、
`warranty_commitment` 和 `dispute_resolution`，用于观察发现流水线能否重新提出这些概念及其
合理变体。这里的删除只改变独立 Discovery 快照，不会删除或修改生产 Attribute 定义。

单合同发现必须直接读取原始 PDF，并依次提取固定 Core 和固定 Attribute；两者结果包含空值
状态并以简洁形式补充上下文。只读取提取结果无法看到尚未建模的信息，不满足字段发现目标。

发现过程遵循以下规则：

- Clause 和 Abstract 不进入该模式的工作流，也不产生对应模型调用；
- 优先保证候选召回、原文证据和可审核性，不承诺候选可直接用于生产；
- 每份合同最多提出 5 个新字段，允许少于 5 个或不提出；
- 新候选先通过结构与固定字段覆盖门禁，再只与同批次新候选进行向量召回；
- Top 5 必须全部由模型判定为 `same`、`related_distinct` 或 `unrelated`；
- 向量检索只缩小局部比较范围，不能自动决定字段身份；
- 单份合同处理期间不得直接修改正式字段目录；
- 同一合同同一字段的重复证据只增加观察次数，不重复增加不同合同出现数；
- 模型不得自动把候选提升为 Core 或正式 Attribute。

---

## 3. Candidate 数据结构

候选生成模型先输出精简的身份提议。它不具备跨合同依据来生成别名、排除概念和真实示例，
因此这些键不进入第一步模型 Schema，也不展示为空列表：

```yaml
field_id: ""
name: ""
meaning: ""
output:                         # 类型描述，不是 JSON Schema
  type: string | number | integer | boolean | date | enum | object | array
extraction_rule: ""
evidence:
  page_number: 1
  source_text: ""
novelty_reason: ""
status: accepted
```

程序校验提议后按 `output.type` 编译正式递归 `output`，再形成带来源、统计、相似字段和审核信息的
候选治理包络。候选池中的建议定义可为与正式字段契约对齐而携带空治理列表，但这些空列表不是
模型生成结果：

```yaml
candidate_id: ""
suggested_definition:
  field_id: ""
  name: ""
  meaning: ""
  aliases: []
  not_meaning: []
  output:
    type: ""
    nullable: true
  extraction_rule: ""
  examples: []
observations:
  - document_id: ""
    page_number: 1
    bounding_box: null
    evidence_hash: ""
comparisons:
  - target_candidate_id: ""
    relation: same | related_distinct | unrelated
    similarity_rank: 1
    reason: ""
identity:
  reused_candidate_id: null
  primary_group_id: ""
statistics:
  occurrence_count: 0
  contract_count: 0
  first_seen_batch: null
  last_seen_batch: null
review:
  status: proposed
  decision: null
  decision_reason: null
  reviewed_at: null
provenance:
  model_version: ""
  prompt_version: ""
  core_catalog_version: ""
  attribute_catalog_version: ""
```

`suggested_definition.output` 是程序编译结果，必须满足
[字段定义契约](../../reference/field-definition-contract.md)，包括递归 object/array、空值语义和枚举
边界。模型只选择 `output.type` 并描述 enum 值、object 子字段或 array 元素；程序统一生成
`nullable`、object 的 `required` 和 `additional_properties=false`，并拒绝类型不完整、参数
错配或互相矛盾的提议，不能让其伪装成可生产字段。

`suggested_definition.extraction_rule` 必须是跨合同规则，只描述确认条件、排除边界、规范化和
缺失/冲突处理；当前合同的页码、条款号、固定章节、版式位置和原句只能保存在
`observations`/`evidence`，不得混入字段定义。发现门禁和组级收敛门禁都会拒绝位置化规则并将
具体原因反馈给模型局部重试一次，不会由程序静默删除词句。

Candidate 的统计和审核数据不写入正式 `attribute.yaml` 字段定义。正式目录保持纯净、稳定
且适合生成 JSON Schema；候选治理记录由发现批次和审核存储独立维护。

候选值可以在单字段验证时短暂存在于内存中，以确认输出 Schema 和字段命中状态，但不进入
Candidate 治理记录或合同正式存储。完整身份选择、分组和第二阶段统计规则见
[字段发现两阶段工作流](../../architecture/field-discovery-workflow.md)。

> **规则与证据分离：** `extraction_rule` 只描述跨合同可复用规则；页码、条款位置与原句只能保存在 `observations` / `evidence`，不能混入正式字段定义。

---

## 4. 正式生产模式

生产模式中的 Attribute 与 Core 一样是固定 Schema。`attribute.yaml` 中的每个字段复用
Core 的正式字段结构：

```yaml
field_id: ""
name: ""
meaning: ""
aliases: []
not_meaning: []
output:
  type: ""
  format: null
  nullable: true
  example: null
extraction_rule: ""
examples: []
```

生产提取必须满足：

- 只输出目录中定义的字段，禁止产生额外键；
- 字段名称、类型、嵌套结构和规范化格式由目录决定，模型不得临时修改；
- 未发现、冲突、歧义和不适用使用字段契约规定的状态和空值表达；
- 每项结果保留 `field_id`、原文、判断摘要、状态和规范值；Attribute Schema 版本由合同级
  `processing.attribute_schema_version` 记录；
- 运行过程中只读字段目录，不执行发现、归并、统计或目录写入；
- Attribute 目录为空时确定性返回空结果，不调用模型。

> **生产边界：** 非空目录必须覆盖所有固定字段，不能以 `attribute: []` 伪装成功；只有显式空目录才允许跳过节点。

如果某组 Attribute 只适用于特定合同类型，可以通过已版本化的 Attribute Profile 管理。
Profile 可以由 Core 分类结果选择，但选择完成后仍只能提取 Profile 内的固定字段。

### 4.1 固定 Attribute 提取管线

```text
共享 PDF 页面 + Attribute 字段目录
  + Core 合同理解地图 + 成功 Core 简洁上下文
  → 逐 Attribute 字段受约束提取（串行）
      ├─ 字段 JSON Schema 校验
      ├─ 值包络校验、跨合同语义门禁与对象状态确定性汇总
      ├─ 未通过语义门禁 → 带校验反馈的单字段局部重试（最多 1 次）
      ├─ 成功字段 → 有序 Attribute 结果
      └─ 单字段结构化失败 → 隔离记录并继续下一字段
  → 字段覆盖与最终阶段校验
  → Attribute StageResult
```

合同理解地图来自 Core 的 Step 1，包含页面主题、主体线索、信息位置和金额/费用线索；它只在
同一合同的内存生命周期内作为定位辅助传递，不进入 Core 对外结果或持久化对象。简洁 Core
上下文只展示 `found` 且规范值非空的字段值，不包含 `raw_value`、`reason`、`status` 等复杂
审计包络。两类上下文都不是证据来源，模型必须回到原始 PDF 图像核验每个 Attribute 结论。

非空目录的最终 `attribute` 是按字段目录顺序排列的列表，每项显式携带 `field_id`；非对象
字段使用 `raw_value/reason/status/value`，对象字段使用程序汇总的 `status/properties`。字段级
失败允许其余字段继续执行，但任一目录字段缺失或包络无效都会使正式阶段失败，禁止输出部分
成功的合同候选。

`reason` 位于同一字段或直属子字段的 `status/value` 之前，先根据 `raw_value` 给出简短的
采用、缺失、冲突或排除判断，再以固定句式承诺后续输出：`found` 使用“所以接下来的
`status=found，value=非 null`”，其他合法状态使用“所以接下来的
`status=实际状态，value=null`”。后续结构化结果必须与该决定一致；复杂数组或对象不在
`reason` 中复制完整规范值。Prompt 要求整个 `reason` 不超过 300 个字符；动态 JSON Schema
和本地 Pydantic 使用 400 个字符的硬上限，保留 100 个字符的生成容错。该摘要用于审计和
提高生成自洽性，不替代 JSON Schema、值包络校验或语义门禁，也不要求模型展开完整思维链。

> **值包络：** `reason` 是紧邻具体字段或子字段的简短审计摘要，必须先于 `status` 与 `value`，并以固定句式承诺后续决定。

---

## 5. 专家治理与目录变更

字段候选的审核结果包括：

| 决策 | 适用条件 | 结果 |
| --- | --- | --- |
| `promote_core` | 跨合同普适、业务关键且定义稳定 | 新版本 `core.yaml` |
| `approve_attribute` | 领域特有但具有稳定提取和使用价值 | 新版本 `attribute.yaml` |
| `merge` | 与已有 Core、Attribute 或候选语义一致 | 合并证据、别名或定义建议 |
| `reject` | 无业务价值、缺乏证据或不可稳定定义 | 保留审计结论，不进入正式目录 |
| `archive` | 暂不采用但值得保留观察 | 后续批次可重新评估 |

任何正式目录变更都必须保留审核人、理由、来源候选、变更前后内容和时间，并触发字段目录
校验、动态 Schema 测试与对应真实合同回归。生产任务只读取启动时固定的目录版本，不能在
处理中切换到新版本。

> **治理边界：** 专家审核是 Candidate 进入任一正式目录的唯一入口；目录变更必须生成新版本并通过字段定义与真实合同回归验证。

---

## 6. 专家预置初始目录

在字段发现算法可用前，项目以当前设备采购类合同集为主要场景，建立一份专家预置的
`0.3/draft` Attribute 目录。它用于实现和验证封闭式生产提取链路，不是模型发现结果，也不
代表字段已经通过真实合同集频次统计。后续发现模式产出的 Candidate 仍须经过专家审核，
才能修改这份目录。

初始目录包含 10 个字段：

| field_id | 名称 | 值类型 | 主要用途 |
| --- | --- | --- | --- |
| `order_numbers` | 订单编号 | `array[string]` | 保存采购订单、销售订单或执行订单编号 |
| `project_numbers` | 项目编号 | `array[string]` | 保存采购、招标、研发、建设等项目编号 |
| `delivery_commitment` | 交付期限 | `object` | 区分明确日期、相对期间和起算条件 |
| `delivery_locations` | 交付地点 | `array[string]` | 保存收货、安装、施工或服务履行地点 |
| `payment_schedule` | 付款安排 | `array[object]` | 结构化付款阶段、条件、比例、金额和期限 |
| `invoice_requirement` | 发票要求 | `object` | 结构化发票类型、开票税率和时点 |
| `acceptance_mechanism` | 验收机制 | `object` | 结构化验收标准、期限和视为验收规则 |
| `warranty_commitment` | 质保承诺 | `object` | 保存质保期间及其起算条件 |
| `performance_security` | 履约保障 | `object` | 保存保证金、质保金或保函安排 |
| `dispute_resolution` | 争议解决方式 | `object` | 保存最终诉讼或仲裁机制及管辖表达 |

### 6.1 选择原则

这批字段遵循以下边界：

- 不重复 Core 已有的合同名称、合同编号、当事人、合同总额、标的和合同期限；
- 面向特定交易场景，允许在大量合同中不存在或不适用；
- 具有稳定定义，并能用于检索、筛选、比较或人工风险审核；
- 只保存条款中的规范化事实，完整条款原文继续由 Clause 负责；
- 同时覆盖数组、对象、对象数组、枚举、日期、数值和布尔值，用于验证递归动态 Schema；
- 不建立 `other_attributes`、`key_terms` 等任意键容器，生产模型不得创造目录外字段。

第一版不纳入联系人、电话号码、银行账号等敏感且易变化的信息，也不纳入任意技术参数。
品牌、型号和主要项目由 Core 的 `subject_matter` 承担；完整付款、验收、违约及争议条款由
Clause 保存。未来可以在服务、软件、技术和租赁合同积累后，再评估知识产权、数据合规、
保密期限或租赁资产等扩展字段。

### 6.2 关键字段边界

- `order_numbers` 与 `project_numbers` 只接受有明确标签或文书角色支撑的编号，不从文件名
  推断，也不无条件复制 Core 的合同编号和关联合同编号；项目名称中的编码式片段不能单独作为
  项目编号。
- `delivery_commitment.deadline_date` 只保存原文明示日期；相对期限保存到 `period_text`，
  模型不自行执行日历计算。
- `payment_schedule` 只采用原文明示的比例和金额，不利用 Core 合同总额反推；银行账号不
  进入该字段。`trigger_text` 只表示付款事件，`due_text` 只表示付款期限或确定付款日，二者
  不得机械复制。
- `invoice_requirement.tax_rate` 必须由发票约定直接支撑，不能复制
  `contract_amount.tax_rate`。
- `acceptance_mechanism.deemed_accepted` 未提及时为 `null`，不能将“未提及”解释为
  `false`。
- `acceptance_mechanism.deadline_text` 是验收流程或异议期限，不是验收款、尾款或其他付款
  期限。
- `warranty_commitment.duration_months` 仅把明确年数或月数换算为月份，按日或条件截止的
  表达只保留原文。
- `performance_security` 不包含普通分期付款或违约金；同一合同存在多种无法由单个对象
  忠实表达的保障时，应输出 `ambiguous`，不能拼接金额与返还条件。
- `dispute_resolution` 中的协商只是前置程序；只有明确诉讼或仲裁安排时才填写最终机制，
  不能根据 Core 主体地址推断具体管辖机构。“买方当地人民法院”等关系性地域表述只保存为
  `jurisdiction_text`，不能填入 `institution_name`。

### 6.3 空值与质量口径

Attribute 的非空率不是完整性指标。每个目录字段都必须得到一个合法终态：

| 状态 | 含义 |
| --- | --- |
| `found` | 合同存在可核对的明确依据，`value` 非空 |
| `not_found` | 字段在当前交易中可能适用，但合同没有写明 |
| `not_applicable` | 根据合同交易性质，该字段明显不适用 |
| `ambiguous` | 存在相关表述，但无法形成唯一可靠结果 |
| `conflicting` | 合同存在不能安全裁决的冲突候选 |

因此，生产质量门禁检查的是“全部 10 个配置字段是否都有合法终态、所有采用值是否有证据、
值是否满足目录 Schema”，而不是要求大部分字段非空。`not_found`、`not_applicable`、
`ambiguous` 和 `conflicting` 都必须令规范值为 `null`，但可以保留最小相关原文用于审核。

### 6.4 当前迁移状态

`data/definitions/attribute.yaml` 已从显式空目录切换为非空 `draft` 目录。生产依赖组装会为
该目录注册固定 Attribute 提取服务；每个字段都必须形成合法终态，不能继续返回 `attribute: []`
伪装成 10 个字段已经完成提取。目录显式为 `status: empty` 且 `fields: []` 时，生产图才会跳过
Attribute 节点并稳定返回空列表。

`EmptyAttributeExtractionService` 仍只服务于显式 `status: empty` 且 `fields: []` 的目录，
并保留独立验证入口
[`experiments/attribute_extraction/run.py`](../../../experiments/attribute_extraction/run.py)。
生产提取器逐字段读取正式目录、使用 PDF 作为事实来源，并复用 Core Step 1 的合同理解地图
和成功 Core 结果的简洁表示作为辅助上下文。

当前 `FieldDiscoveryService` 仍只有应用端口，没有默认算法实现。CLI 选择 `discovery` 时会
在 PDF 渲染与模型连接前抛出 `FieldDiscoveryUnavailableError`，不得把未实现误报为零候选
成功。

> **当前限制：** discovery 默认实现尚未迁移时必须 fail closed；不能通过返回空候选或放宽字段契约伪造可用状态。
