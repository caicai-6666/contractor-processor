# 正式抽取服务

> **适用范围：** 本文定义正式 Core、Attribute、Clause 与 Abstract 抽取服务的职责、输入输出和实验隔离边界。

---

## 模块用途

`infrastructure/extraction/` 保存经过实验验证后迁入正式项目的 Core、Clause、Abstract 算法，
以及 Attribute 空目录策略。正式实现是异步、无状态结果传递且无实验落盘副作用的服务。

Attribute 已建立非空 draft 目录，并拆出字段发现端口与独立图；固定字段提取服务已接入
production，开放字段发现及其全合同集回扫已接入 discovery 批次入口。空策略只适用于显式空目录，详见
[Attribute 双运行模式设计](../architecture/attribute-operating-modes.md)。

> **正式运行边界：** 正式服务只在内存中传递结果，不写实验产物；开放字段发现与固定 Attribute 提取使用独立服务、DTO 和工作流。

---

## 主要职责

- Core：两阶段合同理解和逐字段抽取，保留字段失败隔离与包络校验；
- Empty Core：只在 discovery 的显式空目录下返回 `{}`，不调用 Core 模型；
- Clause：结构发现、边界归并、候选复核和逐单元原文抽取；
- Abstract：固定六栏目生成、栏目业务校验和失败栏目局部重试；
- Attribute：非空目录逐字段受约束提取；显式空目录时确定性返回 `[]`；
- Field Discovery：候选级准入、批次内语义向量召回、关系归并、组级收敛和冻结字段全量回扫；
- Validated adapter：统一准备页面、异步模型客户端、Prompt 版本、共享 MLLM 请求门禁和阶段硬门禁。

---

## 对外接口

四个服务均为协程，接收共享 `PdfExtractionContext` 并返回 `StageResult`：

```python
stage = await core_service.extract(context)
payload = stage.payload
validation = stage.validation
metrics = stage.metrics
```

`ValidatedExtractionPipelines` 在 application 端口外侧消费 validation，只有通过门禁的 payload
才会进入工作流状态。

> **阶段门禁：** 只有通过 `validation` 的 payload 才能进入工作流状态；字段级失败可以隔离，但不完整阶段结果不能越过聚合边界。

---

## 关键实现与设计决策

- 模型请求统一使用 `AsyncOpenAI`；不再保留同步 OpenAI/httpx 客户端。
- 本地 vLLM 的 `httpx.AsyncClient` 固定 `trust_env=False`，避免 localhost 请求误入系统代理。
- PDF 只由 adapter 渲染一次，三个模型阶段共享相同 data URL 页面和客户端。
- 同一合同的 Core、Clause、Abstract 及未来 Attribute 调用共享
  `models.mllm.max_concurrent_requests` 门禁；默认最多 3 个在途请求，异常和取消时配额自动归还。
- Prompt、原始响应、失败记录、metrics 和 validation 不写文件；它们只在当前调用内存中传递。
- 阶段门禁失败时抛出带阶段名、validation 与 metrics 的 `StageValidationError`；正式运行不
  落盘，实验适配器才可将不含 raw response 的诊断摘要写入自己的运行目录。
- 模型的字段级或单元级失败隔离逻辑仍然保留，最终阶段 validation 会把不完整结果拒绝在
  聚合边界之外。
- Abstract 重试仍逐栏目执行，成功栏目不会因为局部失败而被模型重写。
- Attribute 复用 Core Step 1 的合同理解地图，以及只含成功规范值的简洁 Core 上下文；二者
  仅作定位辅助，原始 PDF 始终是 Attribute 的事实来源。
- Attribute 空节点不创建目录和 JSON；它返回带 `empty_catalog` 标志的结构化校验对象。
  统一生产图读取同一目录快照；空 Attribute 时直接省略该节点。
- `FieldDiscoveryService` 端口只接入 `discovery` 工作流；后续
  `AttributeExtractionService` 只接入 `production` 工作流。二者不得用模式分支共用一个
  宽泛的输出 DTO。

> **共享不等于事实依赖：** 页面、模型客户端和请求配额可共享；Core 上下文只为 Attribute 定位，所有 Attribute 事实仍必须直接来源于原始 PDF。

---

## 依赖关系

- 机器规范：`data/definitions/*.yaml`；
- Prompt：各抽取目录下 `prompts/`；
- 模型：本地 vLLM 的 OpenAI 兼容接口；
- 校验：Pydantic、JSON Schema 与项目领域规则；
- 共享输入：`PdfExtractionContext`；
- 返回协议：`StageResult`。

---

## 实验边界与注意事项

`experiments/` 可以调用正式服务并把最终结果保存到 `experiments/outputs/`，也可以在新功能
尚未验证时自行记录 Prompt、响应和分析报告。此类行为不得回流到正式 pipeline。实验分析仍
按 `experiments/experiment-analysis-template.md` 追加到对应运行目录的 `analysis.md`。

修改算法后必须运行完整单元测试和真实 vLLM 集成验证。正式源码的无落盘/无调试输出门禁
由 `test_production_runtime_boundaries.py` 持续检查。

`attribute.yaml` 的首批 draft 字段已经由固定 Schema 提取服务处理。字段发现的候选、统计和
审核产物仍不得进入生产 `StageResult`；下一步应实现独立发现服务，而不是把开放发现逻辑放进
固定 Attribute 节点。

> **实验隔离：** Prompt、响应和诊断仅能由实验包装层保存；未实现的发现能力必须保持独立失败，不能放宽生产 Attribute 节点来伪造支持。
