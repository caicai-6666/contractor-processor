# Core 字段目录

> 状态：`0.9` 初稿，待领域专家基于代表性合同集审核。  
> 适用范围：合同级元数据抽取、RAG 前置过滤和 Attribute 发现边界。  
> 规范地位：[`core.yaml`](core.yaml) 是程序读取与提示词注入的唯一规范源。

## 1. 设计目标

Core 只保存跨合同稳定、业务重要、适合检索过滤且能够追溯到合同证据的信息。付款、交付、服务、验收、质保、违约和争议解决等上下文敏感条款仍由 Clause 层完整保留。

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

## 2. 标量与对象值包络

最终结果的 `fields` 是以 `field_id` 为键的对象。非 object 字段使用
`raw_value/reason/status/value`；object 字段的每个直属子字段独立使用相同包络，
对象外层只保存程序汇总的 `status` 和
`properties`：

```json
{
  "document_id": "设备采购合同",
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

非 object 字段的 `not_found/not_applicable/ambiguous/conflicting` 仍要求
`raw_value/value` 同时为 JSON `null`。字段属性顺序固定为
`raw_value → reason → status → value`。

### object 直属子字段状态

object 的每个直属子字段固定输出 `raw_value → reason → status → value`。数组作为
一个直属子字段处理，不递归包裹数组元素内部属性。

- `found`：存在唯一可采用的子字段值，`value` 非 null。
- `not_found/not_applicable`：`raw_value/value` 都为 null。
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
- 顶层不生成 `reason`，对象外层也不生成 `reason`，避免摘要与实际负责的判断脱节。
- `reason` 是面向审计的简短结论摘要，不是完整 CoT，也不得展开逐步推理。
- 字段包络不再携带独立 `evidence` 或 `conflicts`；原文追溯由 `raw_value` 承担。
- Core 提取实验的 Step 2 固定为单字段调用；结果合并时保持各摘要在原字段或子字段内，不再串联为根级文本。

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

完整定义、别名、反例、输出格式和提取规则见 [`core.yaml`](core.yaml)。

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

生成后的完整 `fields` 对象要求 12 个 Core 键全部存在并禁止额外键；分批实验则为当前
字段子集生成同样严格的 `fields` 约束。当前实验不包含模型复核步骤，Step 2 的合并
结果直接接受相同 Schema 与业务包络校验。

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

## 6. 版本迁移

本版本是破坏性字段库升级，历史结果应显式迁移或重新提取：

| 0.1 | 0.2 |
| --- | --- |
| `contract_type` | 拆为 `transaction_type`、`contract_form`，并新增 `document_role` |
| `effective_date` | 替换为 `effective_mechanism` |
| `contract_term` | 替换为 `contract_validity_period`；旧值若实际是货期或服务期，不得迁移，应进入 Clause |
| `contract_parties[].name` | 改为 `source_name`，新增规范名称、业务角色和信用代码 |
| `contract_amount` | 新增金额性质、税务口径与原文金额 |
| `subject_matter` | 改为受控多分类和结构化项目 |

禁止无证据地把旧 `sales` 映射为新 `goods`，或把旧 `contract_term.duration_text` 直接迁移为合同有效期。

从 0.2 到 0.3：

- 历史 `fields` 数组改为以 `field_id` 为键的对象。
- 包络不再重复输出 `field_id`。
- `raw_value` 统一为最小必要原文字符串或 `null`。
- `evidence.aspect` 替换为机器可校验的 `evidence.value_path`。
- object 的所有子键必须存在；旧结果缺少子键时不能直接视为合法 0.3 数据。

从 0.3 到 0.4：

- 删除 `contract_amount.value.tax_amount`。历史结果迁移时直接移除此键，不再抽取或计算税额；
  `tax_rate` 仅保留合同明示值或满足同口径规则时的确定性结果。

从 0.4 到 0.5：

- 字段包络删除 `evidence` 和 `conflicts`，固定为
  `status/value/raw_value/derivation/confidence`。
- 顶层新增必填 `reason`，位于 `fields` 前，每批用一至两句说明判断依据。
- 历史歧义或冲突候选不会自动迁移；如需保留，应另存为审计产物，不能塞回 0.5 字段包络。

从 0.5 到 0.6：

- 字段包络删除 `derivation` 和 `confidence`，固定为 `raw_value/status/value`；
  生成顺序同样固定为先原文、后状态、再规范值。
- `effective_mechanism.value` 删除 `date_source`，只保留 `date`、`trigger_type` 和
  `trigger_text`。
- 顶层删除 `overall_warnings`，跨字段标识不一致等运行信息只写入
  `02_field_manifest.json`。
- 旧结果迁移时直接删除上述键；不得把它们的历史值拼入 `raw_value` 或 `reason`。

从 0.6 到 0.7：

- 非 object 字段仍保留 `raw_value/status/value`。
- object 字段由 `{raw_value,status,value:{...}}` 改为
  `{status,properties:{子字段:{raw_value,status,value,reason}}}`。
- object 外层 status 不再由模型生成；迁移旧结果不能可靠还原各子字段状态和 reason，
  应重新提取，不得仅按旧对象中的 null 值猜测。
- 新增子字段状态 `out_of_scope`，用于保留“存在相关原文但不属于可采用口径”的候选。
- 顶层 `reason` 从 `fields` 之前移到之后；历史字符串可原样迁移，但不代表具有新的
  子字段粒度。

从 0.7 到 0.8：

- 非 object 字段由 `{raw_value,status,value}` 改为
  `{raw_value,reason,status,value}`。
- object 直属子字段的键顺序由 `raw_value/status/value/reason` 改为
  `raw_value/reason/status/value`；对象外层结构不变。
- 删除根级 `reason`。旧根级摘要无法可靠拆分给具体字段，历史结果如需满足 0.8
  审计粒度，应重新提取而不是机械分配。
- 收紧 `contract_validity_period.start_date`：签订日期或生效日期本身不等于合同
  整体有效期起点，只有合同明确将其定义为整体有效期或合作周期起点时才可采用。

从 0.8 到 0.9：

- 删除 `contract_amount.tax_exclusive_amount`，旧结果迁移时直接移除此子字段。
- 未税价格、未税总额及不含税总价仍保留在 `source_amount_text`，并继续作为税率是否
  允许计算的原文输入，但不再形成独立 Core 值。

## 7. 专家审核重点

- `transaction_type` 与 `subject_matter.categories` 的受控枚举是否覆盖本组织主要业务。
- `master`、`framework` 和 `call_off` 的组织内定义是否需要进一步收紧。
- 主体 `business_role` 是否需要增加本组织特有角色。
- 暂定价、最高限价、据实结算和多币种合同的金额表示是否满足检索需求。
- 代表性合同集中，各字段的非空率、冲突率和人工纠错率是否达到升级冻结标准。
