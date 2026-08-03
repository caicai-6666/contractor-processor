# 字段定义契约

> **规范地位：** 本文是 Core 与正式 Attribute Definition 的解释性字段契约；机器可读的权威来源是 `data/definitions/` 下对应的 YAML 文件。
>
> **适用范围：** 新增、修改、加载或向模型注入字段定义时，均必须遵循本文。Attribute Candidate 的治理数据不属于正式字段定义。

---

## 1. 用途与边界

字段定义不是自由文本，而是生成模型 JSON Schema、校验模型响应和构造检索字段摘要的唯一机器规范。

> **机器规范优先：** 缺少必要约束时，字段目录加载或 Schema 生成必须失败；不能静默补默认值。

Core 与正式 Attribute Definition 使用同一套字段定义。发现模式产生的 Attribute Candidate 在建议定义之外另行记录统计、证据与审核状态，这些治理数据不混入正式字段目录。

字段目录的业务语义与审核入口见：[Core 字段目录](../fields/core/core.md) 与 [Attribute 字段目录](../fields/attribute/attribute.md)。字段发现中候选生成、结构编译与语义门禁的流程见[字段发现两阶段工作流](../architecture/field-discovery-workflow.md)。

---

## 2. 基线结构

```yaml
field_id: ""          # 唯一英文标识，例如 contract_number
name: ""              # 中文名称，例如 合同编号
meaning: ""           # 字段的业务含义
aliases: []            # 合同中可能出现的其他名称
not_meaning: []        # 易混淆、但不属于该字段的概念
output:
  type: ""            # string、number、integer、boolean、date、enum、object、array
  format: null         # 规范化输出格式
  nullable: true       # 缺失时是否允许为 null
  example: null        # 规范化后的示例值
  required: []         # object 必填子字段；所有 properties 键原则上都必须出现
  additional_properties: false
  properties: {}       # object 的递归子字段定义
  items: null          # array 的递归元素定义
  values: {}           # enum 值及各值业务含义
extraction_rule: ""   # 跨合同识别、排除、规范化及缺失/冲突处理规则
examples:              # 正确提取示例
  - source_text: ""
    output: null
```

---

## 3. 结构契约

新增或修改字段前，必须满足以下规则：

| 层级 | 必填键 | 规则 |
| --- | --- | --- |
| 字段 | `field_id`、`name`、`meaning`、`output`、`extraction_rule` | `field_id` 在同一字段库内唯一，使用稳定的 `lower_snake_case`；`aliases`、`not_meaning` 与 `examples` 可省略并默认空数组。 |
| 每个 `output` 节点 | `type`、`nullable` | 每一层（包括 object 子字段和 array items）必须明确空值语义。 |
| `object` | `properties`、`required` | `properties` 不得为空；`required` 必须恰好覆盖全部直属子字段；默认禁止额外属性。 |
| `array` | `items` | `items` 必须是完整的递归 `output` 定义。 |
| `enum` | `values` | 可用列表，或使用“枚举值: 业务含义”的映射；映射形式更适合提示词和人工审核。 |

当前支持 8 种基础类型：`string`、`number`、`integer`、`boolean`、`date`、`enum`、`object`、`array`。`object` 与 `array` 可递归组合，因此可以描述对象数组、嵌套对象等复合值；不支持自定义类型。未列出的 JSON Schema 关键字不会被转换为模型约束，禁止把它们当作有效规则。

| 类型 | 可用约束 |
| --- | --- |
| `string` | `min_length`、`max_length`、`pattern`；`format` 用于向模型说明规范化格式。 |
| `number`、`integer` | `minimum`、`maximum`。 |
| `date` | 固定规范值格式为 `YYYY-MM-DD`。 |
| `enum` | `values`。 |
| `array` | `items`、`min_items`、`max_items`。 |
| `object` | `properties`、`required`、`additional_properties`。 |

`name`、`meaning`、`not_meaning`、`extraction_rule`、`format` 和枚举值说明会被编入模型的 Schema 描述与提示词，表达业务边界；`example` 和 `examples` 用于人工审核与 few-shot 参考。它们不能替代 `type`、`nullable`、`properties` 等结构性约束。

---

## 4. 规则、证据与候选治理

`extraction_rule` 是字段目录级规则，不是某份合同的证据定位。它应说明什么明确事实可以确认字段、哪些相似事实必须排除、如何规范化输出，以及缺失或多候选冲突时如何处理。

> **规则与证据分离：** `extraction_rule` 只描述跨合同可复用的识别与规范化规则；当前合同的页码和原文只能进入候选治理记录的 `evidence`。

因此，规则不得写入页码、条款号、章节序号、固定章节标题、版式位置、当前合同主体、具体字段值或原句。例如“从条款 7 其他约定中提取发票类型”不合法；应写成“仅提取与开票义务直接关联且合同明确约定的发票种类；不得仅凭税率或含税金额推断；未明确则返回空值”。发现实验会在候选门禁和最终组级门禁中执行相同检查，并将失败原因反馈给模型局部重试一次。

Attribute Candidate 额外记录发现次数、出现的不同合同数、首次 / 最近发现批次、来源合同标识、相似字段、审核状态及归并历史。用于专家决策的频次应优先使用“出现的不同合同数”，避免同一合同中的重复表述抬高统计值。这些信息属于候选治理记录，不属于正式 `attribute.yaml` 的字段定义。

---

## 5. 发现模式的定义编译

发现模型不会直接编写正式 `output`，更不会手写 JSON Schema。第一步只输出字段身份提议：`field_id`、`name`、`meaning`、基于 `output.type` 的递归类型描述、`extraction_rule`、证据和新颖性结论；此时不要求模型生成尚无跨合同依据的 `aliases`、`not_meaning` 或 `examples`。

程序按 `output.type` 确定性补齐 `nullable`、object 的 `required` 与 `additional_properties=false` 等正式约束，再编译单字段提取 JSON Schema。组级收敛和专家治理可以在多个候选已有依据后完善别名与排除边界。

字段定义中的 `output` 递归描述规范值结构：object 必须详细声明每个子字段的含义、类型、空值语义与提取规则；array 必须声明 `items`；enum 必须声明 `values`。运行时从该定义动态生成 JSON Schema，禁止用格式字符串或 `Any` 代替复杂值约束。

---

## 6. 结果包络引用

字段定义规定值的业务结构，不单独规定所有提取结果包络。非 object Core 字段按 `raw_value/reason/status/value` 输出；object 字段细化到直属子字段，每个子字段使用相同包络，对象外层只保留由程序确定性汇总的 `status` 和 `properties`。子字段额外支持 `out_of_scope`，用于保留存在但不属于采用口径的原文。

完整的状态、`reason` 与值包络规则以 [Core 字段目录](../fields/core/core.md) 和 [Attribute 字段目录](../fields/attribute/attribute.md) 为准；提示词中 `reason` 的通用编排规则见[提示词工程规范](../architecture/prompt-engineering.md)。
