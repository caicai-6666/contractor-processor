# Attribute 提取质量门禁

> **定位：** 该模块为 production Attribute 提供跨合同可复用的语义质量控制；字段定义负责表达业务边界，运行时仅检查少量稳定不变量。

---

## 模块用途

固定 Attribute 提取既要保持字段目录可演进，也要避免模型把相邻条款、相似标签或关系性表述
误当作事实。本模块为 production Attribute 提供通用的语义质量控制：字段定义表达业务边界，
提示词将边界呈现给模型，运行时在字段 JSON Schema 之外再检查少量跨合同稳定的不变量。

它不针对某份合同或某个编号硬编码。字段目录扩展后，新的字段仍可复用相同的逐字段执行、
证据校验、重试和最终覆盖门禁。

> **适用边界：** 不为单份合同的措辞添加专有分支；无法跨合同稳定验证的规则应留在字段定义与模型判断层。

---

## 执行管线

```text
Attribute Definition + 原始 PDF + Core 辅助上下文
  → 单字段受约束生成
  → JSON Schema / 值包络校验
  → 通用语义门禁
      ├─ 通过 → 合并该字段
      └─ 未通过 → 仅反馈校验原因并重读 PDF 局部重试
  → 字段覆盖与最终阶段校验
```

重试次数来自 `data/definitions/attribute.yaml` 顶层的
`extraction.max_retries_per_field`，当前为 `1`。重试不携带上次模型答案，只携带程序生成的
可执行错误说明，避免错误候选反向锚定模型；PDF 仍是唯一事实来源。

> **重试原则：** 只反馈程序确认的校验原因，并重新读取 PDF 局部内容；不把上一次错误答案作为后续提示词上下文。

---

## 关键实现与设计决策

- 入口为 `infrastructure/extraction/attribute/pipeline.py` 的
  `run_attribute_extraction()`；它按目录顺序隔离字段失败并记录每次尝试的指标。
- `build_attribute_field_prompt()` 从同一个 `FieldDefinition` 生成字段任务、`not_meaning`
  硬排除清单和至多两个正例。正例用于解释边界，不是当前合同事实。
- `validate_attribute_business_rules()` 只处理可跨合同复用的语义不变量：项目名称不是项目编号、
  付款事件与期限必须拆分、付款期限不能充当验收期限、关系性法院描述不是具体机构、质保责任
  描述不能伪装为质保起算条件。
- 结构规则继续由动态 JSON Schema 与 `field_values.py` 的值包络校验负责；业务规则不得用
  Pydantic 模型为每个 Attribute 定制一套类型。
- 任一字段在重试用尽后仍失败时，跳过该字段，其他已成功字段继续形成 production 或 discovery
  的局部 Attribute 结果。工作流把缺失字段、尝试次数和脱敏失败原因保存在诊断元数据中，供
  专家复核或后续定向重试。

> **阶段门禁：** Attribute 以字段为准入单位。技术失败字段不会伪造成 `not_found`；`not_found` 仅表示字段调用成功且模型确认合同没有记载。

---

## 字段定义约定

优先把业务口径写入 `meaning`、`not_meaning`、子字段 `extraction_rule` 与 `examples`。新增
字段只有在存在稳定、可机械检查且跨合同有效的高风险混淆时，才扩展运行时门禁；不得因为
一份合同的措辞去增加专有关键字分支。

当前高风险边界包括：

- 项目名称中的型号、方案代号或编码片段，不等于有项目编号语义的编号；
- 付款条款中的触发事件与履行期限是两个不同子字段；
- “验收款”是付款概念，不自动产生验收流程期限；
- “买方当地人民法院”等可变地域描述保留为管辖原文，不能被伪造成可唯一识别的法院；
- “在质保期内”说明责任范围，不说明质保开始事件。

> **规则归属：** `meaning`、`not_meaning`、`extraction_rule` 与示例优先承载业务口径；只有高风险且可机械验证的混淆才进入通用运行时门禁。

---

## 依赖与验证

- 机器规范：[attribute.yaml](../../data/definitions/attribute.yaml)
- 字段说明：[Attribute 字段设计](../fields/attribute/attribute.md)
- 运行时服务：[正式抽取服务](extraction-services.md)
- 回归测试：`tests/unit/test_attribute_semantic_validation.py` 与
  `tests/unit/test_attribute_metadata.py`

真实合同验证时，应审阅字段状态、`raw_value` 和 `reason`，而不是只比较规范值是否非空。模型
输出与每次局部重试指标只在内存中传递；需要保留原始响应的实验必须在 `experiments/` 边界内
显式执行。

> **审阅依据：** 应同时查看字段状态、`raw_value` 与 `reason`；规范值非空本身不能证明提取正确。
