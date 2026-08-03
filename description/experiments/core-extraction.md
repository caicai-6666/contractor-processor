# Core 抽取实验

> **定位：** 这是正式异步 Core 服务的实验包装器，用于真实 PDF 与 vLLM 回归；它不承载独立的生产算法。

---

## 用途

`experiments/core_field_extraction/run.py` 是正式异步 Core 服务的实验包装器，用于真实 PDF 和
vLLM 回归。正式算法完成合同理解、bullet 上下文转换、逐字段受约束生成、对象状态归并和
覆盖校验。

---

## 使用方式

```bash
python experiments/core_field_extraction/run.py --pdf data/input/example.pdf
```

实验包装器把通过正式阶段门禁的 payload 保存到
`experiments/outputs/core_field_extraction/<run-id>/result.json`。Prompt、raw response、metrics
和校验不由正式算法写文件。

> **产物边界：** 正式服务只返回内存 `StageResult`；Prompt、原始响应、指标和校验等诊断产物只能由实验层显式写入。

---

## 设计决策

- 正式服务通过 `AsyncOpenAI` 调用模型，并返回内存 `StageResult`；
- 单字段失败继续隔离，但最终覆盖不完整会被正式 adapter 拒绝；
- 实验需要的新诊断采集必须在 `experiments/` 内实现；验证后迁入正式项目时去掉 print 和
  报告写入；
- 人工实验分析按模板追加 `analysis.md`，不修改原始 `result.json`。

> **分析原则：** 每次人工结论应追加到对应运行目录的 `analysis.md`，并链接原始响应、指标或校验产物，而不是改写实验结果。

字段语义和提取约束见 [Core 字段说明](../fields/core/core.md)，正式服务边界见
[正式抽取服务](../capabilities/extraction-services.md)。
