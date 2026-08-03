# 统一合同处理工作流

> 当前模块已实现完整的 `production` 拓扑：Attribute 空目录可安全跳过；非空正式目录由
> 固定提取器逐字段处理。字段发现由独立 `DiscoverContractFields` 用例和专用图承担。双模式边界见
> [Attribute 双运行模式设计](../architecture/attribute-operating-modes.md)。

> **适用范围：** 本文只说明生产 `ProcessContract` 用例的异步编排、阶段门禁与对外候选协议；字段发现和专家终审入库均由独立用例负责。

---

## 模块用途

`ProcessContract` 将一份 PDF 异步处理为供专家终审的 `ContractProcessingResult`。正式工作流
只返回内存候选，不创建运行目录、不保存中间文件，也不直接写入 Elasticsearch。
生产用例要求 Core 目录非空，并在渲染 PDF 或连接模型前完成该校验。

当前 `attribute.yaml` 是非空 `0.3/draft` 目录，production 会注册固定 Attribute 提取器并
逐字段处理；只有显式空目录才跳过 Attribute 节点并输出空列表。Attribute 复用 Core Step 1
的合同理解地图及成功 Core 的简洁上下文，但仍以原始 PDF 为唯一事实来源。

> **生产边界：** Core 目录必须非空；只有显式空 Attribute 目录才可跳过节点并返回 `attribute: []`。非空目录不得以空结果伪装成功。

---

## 主要职责

- 在工作线程中计算原始 PDF 的 SHA-256 `document_id` 并完成 PyMuPDF 页面渲染；
- 使用 `AsyncOpenAI` 和 `httpx.AsyncClient` 复用同一份页面输入与模型连接；
- 通过 LangGraph `ainvoke()` 异步执行生产分支：`prepare` 后并发启动 Core、Clause、
  Abstract，Core 完成后才启动 Attribute，并只在 Attribute 目录非空时注册其固定提取节点；
- 直接在内存中传递每个阶段的业务结果、校验状态和指标；
- 任一阶段校验失败时拒绝形成成功候选；
- 返回模型、Prompt 和四份机器规范版本，支持后续终审与复现。

> **成功条件：** 每条已注册业务分支都必须通过自身阶段校验；任一阶段不合法时，工作流只能失败，不能形成部分成功候选。

---

## 对外接口与使用方式

应用接口：

```python
use_case = await build_process_contract(project_root)
result = await use_case.execute(pdf_path)
```

CLI 的同步 `main()` 只是控制台脚本协议要求的外壳，内部通过 `asyncio.run(async_main())`
进入同一条异步应用调用链：

```bash
python -m contract_processor.interfaces.cli.run_single_file \
  --mode production \
  --pdf data/input/example.pdf
```

候选 JSON 只写到标准输出。若实验需要保存结果，应由 `experiments/runtime.py` 在
`experiments/outputs/` 下显式保存，正式用例不会感知实验目录。

> **无落盘边界：** 正式用例只返回内存对象与控制台输出；实验包装层才可以保存结果、诊断和分析材料。

---

## 关键实现与设计决策

- 应用节点依赖 `ContractExtractionPipelines` 异步协议，不依赖 OpenAI、PyMuPDF 或
  LangGraph 具体类型。
- Core → Attribute 保留业务依赖；Clause 和 Abstract 只依赖共享 PDF，在 `prepare` 后与 Core
  并发执行。所有正式模型调用共享 `models.mllm.max_concurrent_requests`（默认 3）门禁，
  因此异步执行不等同于无界并发。
- Attribute 目录为空时，构图器不注册 Attribute 子图，初始状态提供稳定空数组；因此不会
  调用空服务或模型。目录非空时注册固定 Attribute 提取器，字段缺失或包络无效时由阶段门禁
  拒绝生成成功候选。
- 本用例遵循生产封闭世界约束：未来 Attribute 目录非空时只能按固定 Schema 提取，不能在
  `ProcessContract` 中发现、归并或创建字段。
- 生产 `ContractProcessingResult` 保持现有对外协议；发现结果使用独立 DTO，不能写入合同
  正式索引。
- `StageResult` 同时承载 payload、validation 和 metrics，取代“写校验 JSON 后再读取”的
  文件通信方式。
- `ProcessingMetadata` 不再包含本机 `run_directory`，避免部署路径泄漏到 API 或索引协议。
- `ProcessContract` 的 `finally` 始终异步关闭模型客户端，失败时也不会遗留连接。
- 阻塞 PDF/YAML/哈希操作通过 `run_blocking()` 隔离；纯校验、Schema 和领域计算保持同步，
  避免没有收益的 `async` 包装。

> **并发边界：** Core → Attribute 保持真实事实依赖；Clause 与 Abstract 可以并发，但所有模型调用仍受共享请求配额限制。

---

## 依赖关系

- `application/use_cases/process_contract.py`：用例边界；
- `application/workflows/contract_processing.py`：异步节点和端口；
- `infrastructure/orchestration/langgraph_workflow.py`：图拓扑；
- `infrastructure/extraction/validated_pipelines.py`：共享资源与阶段硬门禁；
- `infrastructure/extraction/stage_result.py`：内存阶段结果；
- `async_utils.py`：阻塞 I/O 隔离。

---

## 注意事项

- 自动候选不是正式业务版本；只有专家确认后的对象和原始 PDF 才能交给独立
  `IngestReviewedContract` 入库图保存，合同提取主图不得直接写索引或 PDF。
- 长耗时 HTTP 场景仍应采用“异步受理并返回 job_id + 后台 Worker”；不能因为用例已经异步
  就让上传请求一直等待整个 MLLM 流程。
- 正式抽取源码禁止 `print()`、`write_text()`、运行目录和 raw response 文件依赖；对应静态
  门禁位于 `tests/unit/test_production_runtime_boundaries.py`。

> **终审边界：** 自动候选不是正式业务版本。只有专家确认后的完整对象才能交给独立入库图保存 PDF、索引与向量。
