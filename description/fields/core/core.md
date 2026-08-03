# Core 字段目录

> 状态：`1.1` 初稿已接入正式异步抽取服务，仍待领域专家基于代表性合同集审核。
> 适用范围：合同级元数据抽取、RAG 前置过滤和 Attribute 发现边界。  
> 规范地位：[`data/definitions/core.yaml`](../../../data/definitions/core.yaml) 是程序读取与提示词注入的唯一机器规范源。

---

## 快速导航

- [内部提取管线](#11-core-内部提取管线)：单字段抽取、校验与覆盖门禁。
- [字段总览](#3-字段总览)：合同级 Core 字段的业务范围。
- [递归输出定义](#4-递归输出定义)：机器定义与值包络的关系。
- [历史版本迁移](#6-历史版本迁移)：结构演进与重新提取要求。

---

## 1. 设计目标

Core 只保存跨合同稳定、业务重要、适合检索过滤且能够追溯到合同证据的信息。付款、交付、服务、验收、质保、违约和争议解决等上下文敏感条款仍由 Clause 层完整保留。

字段定义的必填键、8 种基础类型、递归 object/array 规则和可用约束以
[字段定义契约](../../reference/field-definition-contract.md) 为准；本文件只补充 Core 的业务
语义、值包络和字段级提取规则。

> **机器规范优先：** [`data/definitions/core.yaml`](../../../data/definitions/core.yaml) 是唯一机器规范源；本文只解释业务边界、值包络与审核重点。

`0.2` 解决了字段语义边界；`0.3` 进一步解决复杂值只有格式字符串、子字段不能被 JSON Schema 强制约束的问题：

- object 必须递归声明 `properties`、`required` 和 `additional_properties`。
- array 必须递归声明 `items`，枚举必须声明带含义的 `values`。
- 每个子字段独立描述名称、含义、反例、空值语义、格式和提取规则。
- 运行时直接从 YAML 生成逐字段 JSON Schema，不再以 `Any` 作为实际约束。

`0.4` 移除不作为检索元数据使用且容易诱发跨口径计算的
`contract_amount.tax_amount`。

`0.5` 将字段包络精简为五个业务键，并把一至两句判断摘要提升到批次级
`reason`。该摘要用于审计模型采用了什么判断依据，不要求模型展示完整思维链。

`0.6` 删除字段级 `derivation`、`confidence`、生效机制中的 `date_source` 和顶层
`overall_warnings`，只保留直接参与消费或追溯的结构。

`0.7` 将 object 字段细化为直属子字段决策包络，新增 `out_of_scope`，并由应用层根据
子字段状态确定性汇总对象外层状态。字段级 `reason` 统一移到 `fields` 之后，避免摘要
在状态生成前形成锚定。

`0.8` 将简短判断摘要直接绑定到它所判断的值：非 object 字段自身携带 `reason`，
object 字段由每个直属子字段携带 `reason`，并统一采用
`raw_value → reason → status → value` 的生成顺序。根级 `reason` 被删除。

`0.9` 删除 `contract_amount.tax_exclusive_amount`。合同中的未税价格或未税总额仍由
Step 1 穷举，并保留在 `source_amount_text` 中，但不再形成独立 Core 子字段。

`1.0` 将 `contract_number` 确定为合同必填且唯一的业务标识。
字段定义改为 `nullable: false`；提取层仍允许如实报告缺失、歧义或冲突，
但任一非 `found` 结果都是合同级校验失败，不得生成最终 Core 产物、摘要或索引记录。

`1.1` 修正上述身份假设：实际合同可能没有合同编号，编号也不能可靠承担文件唯一性。
`contract_number` 恢复为可空业务字段；所有合同级产物改用原始 PDF 文件字节的 SHA-256
作为 `document_id`。编号缺失、冲突或重复不再阻断最终产物。

---

## 1.1 Core 内部提取管线

```text
共享 PDF 页面 + Core 字段目录
  → Step 1：合同整体理解
  → 合同理解 JSON Schema / 结构校验
  → 精简 bullet 上下文
  → Step 2：逐字段受约束提取（串行）
      ├─ 字段 JSON Schema 校验
      ├─ 业务包络校验与确定性规范化
      ├─ 成功字段 → 合并 Core fields
      └─ 单字段结构化失败 → 隔离记录并继续下一字段
  → 字段覆盖、必填字段与最终业务校验
  → Core StageResult
```

Step 1 只建立文书概览、页面主题、主体线索、信息位置以及金额/费用原文等共享理解，不能直接
生成最终 Core 值。Step 2 始终以原始 PDF 为事实来源，并把经过校验的 Step 1 bullet 上下文
作为定位辅助；每次调用只允许返回一个字段，避免多个字段之间互相干扰。

字段级结构化失败不会覆盖已经成功的字段，但会进入该字段的诊断指标。阶段末端会检查目录
覆盖、值包络和所有非空字段要求；任一校验未通过时，正式适配器拒绝将 Core 结果交给后续
工作流。模型响应、提示词和诊断只在内存中传递，实验入口如需落盘应在 `experiments/` 内完成。

> **事实来源：** Step 1 的理解地图只用于定位；Step 2 的每个最终字段都必须直接依据原始 PDF，不得由上下文补写或否定事实。

---

## 2. 标量与对象值包络

最终结果的 `fields` 是以 `field_id` 为键的对象。非 object 字段使用
`raw_value/reason/status/value`；object 字段的每个直属子字段独立使用相同包络，
对象外层只保存程序汇总的 `status` 和
`properties`：

```json
{
  "document_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "fields": {
    "effective_mechanism": {
      "status": "found",
      "properties": {
        "date": {
          "raw_value": null,
          "reason": "合同未明示或可靠确定具体生效日期。",
          "status": "not_found",
          "value": null
        },
        "trigger_type": {
          "raw_value": "本合同自双方签字盖章之日起生效",
          "reason": "原文同时要求签字和盖章。",
          "status": "found",
          "value": "on_signing_and_seal"
        },
        "trigger_text": {
          "raw_value": "本合同自双方签字盖章之日起生效",
          "reason": "保留生效条件原文。",
          "status": "found",
          "value": "本合同自双方签字盖章之日起生效"
        }
      }
    }
  }
}
```

### 非 object 字段状态

| 状态 | 含义 | `value` 规则 |
| --- | --- | --- |
| `found` | 找到唯一、可采用的结果 | 非空 |
| `not_found` | 已检查可见页面但未发现 | 必须为 `null` |
| `ambiguous` | 找到相关表述但语义或归属无法消歧 | 必须为 `null` |
| `conflicting` | 存在互不相容的候选值 | 必须为 `null` |
| `not_applicable` | 字段对该合同类型明确不适用 | 必须为 `null` |

非 object 字段的 `not_found/not_applicable/ambiguous/conflicting` 要求
`value` 为 JSON `null`。`raw_value` 可为 null；存在相关但不足以形成可采用值的原文时，
可保留最小相关原文以支持审计。字段属性顺序固定为
`raw_value → reason → status → value`。

> **包络顺序：** `reason` 是绑定到当前值的简短审计摘要，不是完整 CoT；它必须位于 `status` 与 `value` 之前，并以固定格式承诺后续决定。

### object 直属子字段状态

object 的每个直属子字段固定输出 `raw_value → reason → status → value`。数组作为
一个直属子字段处理，不递归包裹数组元素内部属性。

- `found`：存在唯一可采用的子字段值，`value` 非 null。
- `not_found/not_applicable`：`value` 为 null；`raw_value` 可为 null，也可保留说明“有相关语境但无可采用值”的最小原文。
- `ambiguous/conflicting`：`value` 为 null，`raw_value` 保留相关候选原文，
  `reason` 说明无法采用的原因。
- `out_of_scope`：存在相关原文，但不属于该子字段允许采用的业务口径；
  `raw_value` 必须保留被排除内容，`value` 为 null，`reason` 说明口径边界。

模型不输出 object 外层 status。应用层按以下优先顺序汇总：

1. 任一直属子字段为 `found` → 对象 `found`；
2. 否则存在 `conflicting` → 对象 `conflicting`；
3. 否则存在 `ambiguous` → 对象 `ambiguous`；
4. 全部为 `not_applicable` → 对象 `not_applicable`；
5. 其他组合（包括只有 `not_found/out_of_scope`）→ 对象 `not_found`。

这样局部空值、冲突或口径排除不会覆盖已经明确的其他子字段。例如合同总额明确而税率
无法可靠确定时，`contract_amount.status` 仍可由明确的金额子字段汇总为 `found`。

生成 Schema 仍保持平坦属性约束，不使用 `allOf` 等组合关键字表达状态机，以兼容
实际约束生成后端；跨属性关系和对象状态汇总由应用层执行。

日期、币种、名称和受控分类仍按各字段规则规范化；允许计算的字段必须满足规则规定的
输入与口径条件。删除 `derivation` 不降低这些业务约束，确定性程序仍应复算可计算值。

### 字段判断摘要

- 非 object 字段自身携带 `reason`；object 字段只由各直属子字段携带 `reason`。
- `reason` 位于同一包络的 `status` 之前，依据已经生成的 `raw_value` 给出一至两句判断摘要。
- `reason` 必须在结尾明确承诺后续输出决定：`found` 使用“所以接下来的
  `status=found，value=非 null`”；空值状态使用“所以接下来的 `status=实际状态，value=null`”。
  后续结构化 `status/value` 必须与该决定一致。
- Prompt 要求整个 `reason`（含固定输出决定）不超过 300 个字符；动态 JSON Schema 和本地
  Pydantic 使用 400 个字符的硬上限，保留 100 个字符的生成容错。复杂数组或对象不在
  `reason` 中重复完整值。
- 顶层不生成 `reason`，对象外层也不生成 `reason`，避免摘要与实际负责的判断脱节。
- `reason` 是面向审计的简短结论摘要，不是完整 CoT，也不得展开逐步推理。
- 字段包络不再携带独立 `evidence` 或 `conflicts`；原文追溯由 `raw_value` 承担。
- Core 提取实验的 Step 2 固定为单字段调用；结果合并时保持各摘要在原字段或子字段内，不再串联为根级文本。

---

## 3. 字段总览

| field_id | 名称 | 主要筛选用途 |
| --- | --- | --- |
| `contract_title` | 合同名称 | 展示与标题检索 |
| `contract_number` | 当前合同编号 | 精确定位 |
| `document_role` | 合同文书角色 | 主合同、补充、变更、续签、终止等 |
| `related_contract_numbers` | 关联合同编号 | 关联主合同和协议链 |
| `transaction_type` | 交易类型 | 按主要交易对象分类 |
| `contract_form` | 合同形态 | 确定交易、框架、主协议、执行文件 |
| `contract_parties` | 合同相关方 | 主体身份、原文称谓和业务角色 |
| `signing_date` | 签订日期 | 签订时间 |
| `effective_mechanism` | 生效机制 | 生效日期与触发条件 |
| `contract_validity_period` | 合同有效期 | 合同整体效力或合作周期 |
| `contract_amount` | 合同金额 | 金额性质、币种和税务口径 |
| `subject_matter` | 合同标的 | 标的概括、受控分类和主要项目 |

完整定义、别名、反例、输出格式和提取规则见
[`data/definitions/core.yaml`](../../../data/definitions/core.yaml)。

`contract_number` 是可空业务字段，只用于展示、精确检索和关联分析，不承担唯一身份职责。
缺失时如实返回 `not_found + null`；重复编号可以属于不同原始文件。合同级唯一性由程序对
原始 PDF 文件字节计算的 SHA-256 `document_id` 保证，完整规则见
[合同文档身份协议](../../architecture/document-identity.md)。

> **身份边界：** `contract_number` 是可空业务字段；所有合同级产物的文件身份始终使用原始 PDF SHA-256 `document_id`。

---

## 4. 递归输出定义

简单值直接声明 `type`、`nullable` 和格式；复杂对象必须声明完整子字段：

```yaml
output:
  type: object
  nullable: true
  required: [amount, currency, amount_type]
  additional_properties: false
  properties:
    amount:
      name: 采用的合同总金额
      meaning: 依据 amount_type 确定的合同级总额
      type: number
      nullable: true
      not_meaning: [单价, 预付款, 违约金]
      extraction_rule: 仅固定、暂定或最高限价等合同级总额可填写。
```

所有 `properties` 键都应列入 `required`，但允许按业务定义使用 `nullable: true`。键缺失表示结构错误；键存在且值为 `null` 表示已经执行该子字段提取但没有可靠结果。

[`core_extraction.py`](../../../src/contract_processor/application/schemas/core_extraction.py) 负责递归转换：

- object → `properties + required + additionalProperties: false`；
- array → 强类型 `items`；
- enum → 受控 `enum`；
- date → `YYYY-MM-DD` 正则；
- 数值、字符串 → 最小值、最大值、长度和格式约束；
- 每个字段 → 绑定自身 value Schema 的统一包络。

模型单字段 Schema 只包含 `fields`，不包含 `document_id`。最终 Core 合并产物中的
`document_id` 由程序计算后注入，模型无法使用合同编号、标题或文件名替代。

生成后的完整 `fields` 对象要求 12 个 Core 键全部存在并禁止额外键；分批实验则为当前
字段子集生成同样严格的 `fields` 约束。当前实验不包含模型复核步骤，Step 2 的合并
结果直接接受相同 Schema 与业务包络校验。

> **结构约束：** 模型只返回字段值包络；`document_id` 由程序注入，完整字段对象由动态 Schema 和本地业务校验共同约束。

---

## 5. 关键边界

### 文书角色、交易类型和合同形态

三者是独立维度。例如，一份“采购框架协议补充协议”可以同时是：

```json
{
  "document_role": "supplementary_agreement",
  "transaction_type": "goods",
  "contract_form": "framework"
}
```

不得再使用单一 `contract_type` 在这三个维度之间择一。

### 合同有效期与履约期限

`contract_validity_period` 只表示合同整体有效期或整体合作期。以下内容进入 Clause：

- 交货期、到货期限；
- 单项服务完成期限；
- 施工工期和里程碑；
- 付款账期、验收期、质保期；
- 保密义务的存续期限。

没有整体有效期时，该字段应为 `not_found`，不能为了提高覆盖率而选择“最像期限”的条款。

“签字并盖章”属于生效机制的组合触发条件，统一使用
`effective_mechanism.trigger_type=on_signing_and_seal`；不得仅选择 `on_signing`
或 `on_seal` 而丢失另一项必要条件。

生效日期还受原文证据不变量约束。明确日期、签订之日和最后签署日可按字段规则归一化；
签字并盖章、付款、审批或其他条件的完成日期必须直接出现在生效条款证据中，不能把合同
签订日期当作这些条件实际完成的日期。

### 主体原文与规范化

`source_name` 和 `source_designation` 忠实保留合同原文。`normalized_name`、`business_role` 和统一社会信用代码是并行的规范化信息，不能覆盖原文，也不能仅按甲乙方顺序推断。

### 金额

先判断 `amount_type`，再决定 `amount` 是否应有值。`framework_no_total`、`unit_price_only` 和 `settlement_based` 都可能合法地具有 `amount: null`，但整个字段仍为 `found`。

金额提取遵循“原文明示优先于计算和推断”。合同明确列示的总价、含税属性和税率直接
忠实提取，不能因为模型无法复算或算术结果不吻合而丢弃明示值。

`contract_amount` 不再输出独立未税金额。合同中的“未税价格”“未税总额”或“不含税
总价”仍属于必须保留的金额原文，应写入 `source_amount_text`，但不能创建字段定义之外
的额外属性。原文未给税率时，只有合同明确给出的含税、未税金额覆盖范围完全一致，才
允许计算 `tax_rate`；范围不同或不明确时禁止反推税率，也不得计算或输出税额。

### 标的

`categories` 使用受控枚举承担稳定过滤；`summary` 和 `items.source_name` 承担可读性与原文追溯。`items.brand` 保存合同原文明示且能与当前项目可靠对应的品牌或厂牌；该键固定输出但允许为 `null`，不得依据型号、生产厂家、供应商名称或外部知识推断品牌。行业专有分类在积累足够 Attribute 统计后再由专家扩展，不允许模型即时创造枚举。

---

## 6. 历史版本迁移

历史结果的迁移记录、破坏性变更与重新提取要求已移至[Core 字段目录版本迁移记录](core-version-history.md)。当前字段契约无法由旧产物可靠还原时，必须重新读取原始 PDF 并重新提取；不得依据缺失字段、旧空值或历史摘要进行推断。

---

## 7. 专家审核重点

- `transaction_type` 与 `subject_matter.categories` 的受控枚举是否覆盖本组织主要业务。
- `master`、`framework` 和 `call_off` 的组织内定义是否需要进一步收紧。
- 主体 `business_role` 是否需要增加本组织特有角色。
- 暂定价、最高限价、据实结算和多币种合同的金额表示是否满足检索需求。
- 代表性合同集中，各字段的非空率、冲突率和人工纠错率是否达到升级冻结标准。
