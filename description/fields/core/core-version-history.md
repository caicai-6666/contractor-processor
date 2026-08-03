# Core 字段目录版本迁移记录

> **用途：** 本文保留 Core 字段目录的历史结构变更、迁移限制与重新提取要求。当前有效的字段规则、值包络和审核重点见 [Core 字段目录](core.md)。

---

## 迁移通则

历史结果应显式迁移或重新提取。无法依据原始证据可靠还原的字段、子字段状态或 `reason`，不得机械补全；应重新提取或作为历史审计产物保留。

---

## 0.1 → 0.2

| 旧字段 | 新字段或处理方式 |
| --- | --- |
| `contract_type` | 拆为 `transaction_type`、`contract_form`，并新增 `document_role`。 |
| `effective_date` | 替换为 `effective_mechanism`。 |
| `contract_term` | 替换为 `contract_validity_period`；旧值若实际是货期或服务期，不得迁移，应进入 Clause。 |
| `contract_parties[].name` | 改为 `source_name`，新增规范名称、业务角色和信用代码。 |
| `contract_amount` | 新增金额性质、税务口径与原文金额。 |
| `subject_matter` | 改为受控多分类和结构化项目。 |

禁止无证据地把旧 `sales` 映射为新 `goods`，或把旧 `contract_term.duration_text` 直接迁移为合同有效期。

---

## 0.2 → 0.3

- 历史 `fields` 数组改为以 `field_id` 为键的对象。
- 包络不再重复输出 `field_id`。
- `raw_value` 统一为最小必要原文字符串或 `null`。
- `evidence.aspect` 替换为机器可校验的 `evidence.value_path`。
- object 的所有子键必须存在；旧结果缺少子键时不能直接视为合法 0.3 数据。

---

## 0.3 → 0.4

- 删除 `contract_amount.value.tax_amount`。历史结果迁移时直接移除此键，不再抽取或计算税额；`tax_rate` 仅保留合同明示值或满足同口径规则时的确定性结果。

---

## 0.4 → 0.5

- 字段包络删除 `evidence` 和 `conflicts`，固定为 `status/value/raw_value/derivation/confidence`。
- 顶层新增必填 `reason`，位于 `fields` 前，每批用一至两句说明判断依据。
- 历史歧义或冲突候选不会自动迁移；如需保留，应另存为审计产物，不能塞回 0.5 字段包络。

---

## 0.5 → 0.6

- 字段包络删除 `derivation` 和 `confidence`，固定为 `raw_value/status/value`；生成顺序同样固定为先原文、后状态、再规范值。
- `effective_mechanism.value` 删除 `date_source`，只保留 `date`、`trigger_type` 和 `trigger_text`。
- 顶层删除 `overall_warnings`；当时实验的跨字段运行信息只写入实验 `02_field_manifest.json`。当前正式服务不写该文件。
- 旧结果迁移时直接删除上述键；不得把它们的历史值拼入 `raw_value` 或 `reason`。

---

## 0.6 → 0.7

- 非 object 字段仍保留 `raw_value/status/value`。
- object 字段由 `{raw_value,status,value:{...}}` 改为 `{status,properties:{子字段:{raw_value,status,value,reason}}}`。
- object 外层 status 不再由模型生成；迁移旧结果不能可靠还原各子字段状态和 reason，应重新提取，不得仅按旧对象中的 null 值猜测。
- 新增子字段状态 `out_of_scope`，用于保留“存在相关原文但不属于可采用口径”的候选。
- 顶层 `reason` 从 `fields` 之前移到之后；历史字符串可原样迁移，但不代表具有新的子字段粒度。

---

## 0.7 → 0.8

- 非 object 字段由 `{raw_value,status,value}` 改为 `{raw_value,reason,status,value}`。
- object 直属子字段的键顺序由 `raw_value/status/value/reason` 改为 `raw_value/reason/status/value`；对象外层结构不变。
- 删除根级 `reason`。旧根级摘要无法可靠拆分给具体字段，历史结果如需满足 0.8 审计粒度，应重新提取而不是机械分配。
- 收紧 `contract_validity_period.start_date`：签订日期或生效日期本身不等于合同整体有效期起点，只有合同明确将其定义为整体有效期或合作周期起点时才可采用。

---

## 0.8 → 0.9

- 删除 `contract_amount.tax_exclusive_amount`，旧结果迁移时直接移除此子字段。
- 未税价格、未税总额及不含税总价仍保留在 `source_amount_text`，并继续作为税率是否允许计算的原文输入，但不再形成独立 Core 值。

> **迁移底线：** 当前字段契约无法由旧产物可靠还原时，必须重新读取原始 PDF 并重新提取；不得依据缺失字段、旧空值或历史摘要进行推断。
