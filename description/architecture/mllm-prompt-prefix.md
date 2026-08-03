# MLLM Prompt 共享前缀设计

> 状态：已由 Core、Clause、Abstract、Attribute 正式服务与统一工作流共同实现。

---

## 快速导航

- [Prompt 分层](#2-prompt-分层)：P0–P3 的稳定性与内容归属。
- [一致性要求](#3-严格一致性要求)：共享前缀的输入与语义约束。
- [工作流编排](#5-工作流编排)：页面、上下文与节点间复用方式。
- [可观测性](#7-可观测性与验收)：性能与正确性的验证边界。

---

## 1. 用途

Core 字段提取、Clause 条款提取、Contract Summary（Abstract）和固定 Attribute 提取都直接读取同一份合同 PDF。四者应共享完全一致的多模态输入前缀，使 vLLM 可以复用公共 Prompt 和页面图像对应的前缀缓存，同时避免在多个模块中重复渲染 PDF。

共享前缀只是一项性能优化，不是任务正确性的依赖。缓存未命中、被淘汰或被禁用时，Core、Clause、Contract Summary 和 Attribute 必须仍能根据完整输入得到正确结果。

提示词内部的通用内容顺序、`reason` 固定收束格式及结构化信息呈现方式见
[提示词工程规范](prompt-engineering.md)。本文只规定 P0～P3 的多模态输入分层与前缀缓存边界。

> **性能而非正确性：** 前缀缓存可以失效、被淘汰或关闭；所有业务节点仍必须凭完整输入获得同样的校验语义。

---

## 2. Prompt 分层

统一工作流采用以下前缀层次：

| 层 | 内容 | 共享范围 |
| --- | --- | --- |
| P0 | 完全一致的 system message | Core、Clause、Contract Summary、Attribute |
| P1 | PDF 阅读、安全边界、确定性页面可见范围和物理页码规则 | Core、Clause、Contract Summary、Attribute |
| P2 | 按物理页码排列的完整 PDF 页面图像 | Core、Clause、Contract Summary、Attribute |
| P3 | 任务专属规则、字段定义和输出要求 | 各节点独立 |

对应消息结构为：

```text
Core Map     = P0 + P1 + P2 + CoreMapTask
Clause Map   = P0 + P1 + P2 + ClauseMapTask
Summary      = P0 + P1 + P2 + SummaryTask
Attribute    = P0 + P1 + P2 + AttributeFieldTask
```

后续 Core 单字段提取与 Clause 边界复核、条款提取，也应在各自任务族内保持最长稳定前缀，把字段或条款的可变说明放在消息末尾。

---

## 3. 严格一致性要求

Prefix caching 要求从消息开头开始的连续输入保持一致，因此“使用相同图片”本身并不等于共享图像前缀。P0 或 P1 中即使只有一个字不同，或者某个任务在图像前插入了自己的页面说明，后续图像都不再属于同一连续前缀。

工作流必须保证：

- 四个节点使用同一 system message，不维护近似但不同的副本；
- P1 只从一个公共 Prompt 文件读取；
- 同一合同只渲染一次，四个节点复用同一组内存图像对象、顺序和编码；
- 图像分辨率、缩放矩阵、色彩空间、透明通道和编码格式完全一致；
- P1 的页面范围使用一个确定性渲染函数，不能由三个实验分别组织措辞；
- 任务专属规则、字段指南、Schema 说明和动态内容全部位于 P3；
- 四个节点使用相同的整份 PDF 页集合，不允许一个节点静默截断而其他节点读取全文。

结构化输出的 JSON Schema 和业务校验仍由各节点独立维护。不能为了延长共享前缀而合并互不相同的业务协议。

> **稳定前缀，可变后缀：** P0～P2 必须逐字节一致；字段规则、Schema 说明和其他任务专属动态内容只能放在 P3。

---

## 4. 地图识别边界

### 4.1 Core Map 与 Clause Map

Core Map 和 Clause Map 共享 P0～P2，但不合并为一个大型模型响应：

- Core Map 关注主体、合同编号、日期、金额、标的和字段位置；
- Clause Map 关注标题、编号、标签、分点、视觉顺序和跨页边界；
- 两者的覆盖口径、Schema 和后续校验不同，强行合并会放大单次输出并耦合失败范围。

真正相同的阅读、安全、页码与证据规则集中在 P1。具体字段和条款候选规则留在各自 P3，
避免为了追求更长缓存前缀而把不相关语义注入其他任务。

### 4.2 Abstract 与 Attribute

Contract Summary 不消费 Core Map 或 Clause Map 的模型结果。它与二者共享 PDF 前缀，但继续直接依据原 PDF 生成六栏目摘要，从而保持信息来源和失败边界独立。

Attribute 复用 Core Map 的确定性 bullet 表示及成功 Core 的简洁规范值作为 P3 定位辅助；二者
都位于图像之后，且不是 Attribute 的事实来源。Attribute 仍直接读取完整原 PDF，不能因 Core
遗漏或冲突而跳过、补写或否定字段事实。

> **共享不等于依赖：** Abstract、Attribute 和 Clause 都必须保留各自的事实来源、Schema、业务校验与失败边界；辅助上下文不能升级为事实来源。

### 4.3 字段发现的受限例外

字段发现第一大步统一实验有一个有意的例外：候选生成对每份合同只调用一次，而发现任务、冻结的
Discovery Core/Attribute 定义与输出约束在整个批次中稳定不变。因此实验将这些静态规则置于
图像前，把页数说明和该合同的固定字段状态置于图像后，以便不同合同从首张图像开始分叉时仍可
复用最长静态文本前缀。此例外仅适用于实验候选生成，不改变正式 Core、Attribute、Clause 或
Abstract 的 P0～P3 顺序。

### 4.4 纯文本关系判别

字段发现的候选归属三分类不属于 PDF 理解任务。候选在进入该节点前已经通过 PDF 来源证据门禁，
所以每个 Top 候选仅与当前新字段进行一次纯文本定义判别，不传页面图像、页码或合同字段值。
同一当前字段的逐对调用顺序执行，使“关系规则 + 当前字段定义”保持为稳定文本前缀，待比较的
一个候选定义作为后缀；程序必须收齐 Top 5 的全部结果后才决定身份或分组。

---

## 5. 工作流编排

正式工作流应新增统一的 PDF 输入与 Prompt 编排职责：

```text
PDF 校验
  → 统一全量渲染为 PdfImageBundle
  → 构建 P0 + P1 + P2
  → Core Map（预热公共多模态前缀）
  → Core 单字段提取 → Attribute 单字段提取（复用 Core Map）
  → Clause Map（复用公共多模态前缀）
  → Contract Summary（复用公共 PDF 前缀）
  → 各任务后续步骤
  → 聚合三个独立结果和运行指标
  → 专家最终校验并直接修正
  → 确认后一次性落盘
```

前缀缓存优化不形成业务依赖。生产图在 prepare 后并发提交 Core、Clause 和 Abstract；每次
调用都受共享 `models.mllm.max_concurrent_requests` 配额限制，默认最多 3 个在途请求。部署方
应以实际 vLLM 观测决定是否降低该值，而不能通过把 Clause、Abstract 串在 Core 后面来控制
资源。

编排层只负责输入复用、调用顺序和结果聚合，不解释 Core、Attribute、Clause 或摘要业务字段。四个节点分别执行自己的 JSON Schema、业务校验和失败处理；正式工作流不增加跨产物自动一致性校验。聚合后的候选由专家对照 PDF 最终校验，具体边界见[合同信息化处理工作流](contract-information-workflow.md)。

> **编排边界：** 编排层只复用输入、安排调用与聚合结果；不得解释字段业务含义、合并各节点 Schema，或自动裁决跨产物事实。

---

## 6. 接口与内存结果

### 6.1 共享 PDF 输入

统一输入对象至少包含：

```text
PdfImageBundle
  document_id                 # 原始 PDF 文件字节的 SHA-256
  source_pdf_path
  source_page_count
  rendered_pages[]
    physical_page
    data_url
    image_bytes
  render_profile
```

### 6.2 阶段内存结果

正式工作流通过内存对象传递：

```text
StageResult
  payload                       # 当前阶段业务结果
  validation                    # 当前阶段硬门禁依据
  metrics                       # 调用耗时、token 和缓存指标
  artifacts                     # 同一合同内部阶段使用的只读辅助信息，不进入对外 DTO
```

合同页面只保留在内存。正式流程不保存页面、Prompt 或响应；显式排障材料只能由实验包装层
写入 `experiments/outputs/`。

> **内存边界：** 页面、Prompt 和模型响应只在合同处理生命周期内使用；正式流程不得将它们作为业务产物保存。

---

## 7. 可观测性与验收

每次模型调用统一记录：

- 节点名、Prompt 层版本和任务后缀版本；
- prompt、completion 和 total token；
- 服务返回时记录 cached token 或等价缓存指标；
- 图像数量、图像总字节数、耗时和上下文剩余量；
- 是否为预热调用及其预期复用层级。

迁移验收至少覆盖：

1. 四个节点的 P0、P1、P2 在序列化后完全一致；
2. 所有任务语义只出现在图像后的 P3；
3. 同一合同只执行一次 PDF 渲染；
4. 页数超过安全上限时四个节点采用相同失败策略；
5. 关闭 prefix caching 后，四个业务结果和开启时保持同一校验语义；
6. 四个节点中任意一个失败，不会生成聚合成功结果或正式持久化副作用。

---

## 8. 当前实现

- `application/prompts/pdf_prefix.py` 是 system、公共规则与页面范围的唯一组装入口；
- 四个正式算法的消息构造均为相同 P0、P1、P2 后追加 P3，回归测试比较完整消息前缀；
- 正式工作流只渲染一次 PDF，并向四个服务注入同一组内存图像和同一个模型客户端；
- 三个实验薄入口调用正式异步 adapter；统一工作流和实验入口均只渲染一次；
- 正式工作流与实验入口均拒绝静默截断，超过安全页数时在抽取前失败；
- `prompt_version` 对正式 Prompt 文件内容计算 SHA-256，随聚合结果保存。

是否实际命中 KV/prefix cache 仍以 vLLM 服务指标为准；输入一致是可复用前缀的必要条件，
不是缓存命中的充分证明。

> **验收结论：** 输入一致只证明“可以复用”；是否实际命中缓存必须以服务端指标为准。
