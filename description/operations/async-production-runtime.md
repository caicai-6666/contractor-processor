# 异步生产运行时与副作用边界

> **适用范围：** 本文定义正式抽取与终审入库的异步调用、阻塞 I/O 隔离和副作用边界；实验落盘与控制台输出不构成正式工作流状态。

---

## 模块用途

本模块说明正式项目从同步实验实现迁移到异步生产调用链后的统一约束。

---

## 异步调用链

```text
CLI async_main / future FastAPI route
  → await build_process_contract
  → await ProcessContract.execute
  → await LangGraph.ainvoke
  → await ContractProcessingNodes
  → await ValidatedExtractionPipelines
  → 并发 await Core / Clause / Abstract service
  → Core 完成后 await Attribute service
  → await AsyncOpenAI / httpx.AsyncClient
```

独立入库图的 Elasticsearch mapping 初始化、四向量写入、回读和 PDF 存储均采用异步公共
接口；正式 ES 写入使用异步 Client 协议。配置加载、Prompt 读取与版本哈希、文档身份计算、字段目录和 CLI 路径解析均暴露异步
公共接口。PyMuPDF、SHA-256、YAML 和普通文件读取本身是阻塞 API，其私有同步实现统一通过
`run_blocking()` 放到进程级有界工作线程池，不直接占用事件循环；执行器在进程退出时回收。

> **异步边界：** 网络、磁盘和长耗时编排的公共边界必须异步；PyMuPDF、SHA-256、YAML 等阻塞实现只能通过 `run_blocking()` 隔离。

---

## 副作用边界

- 正式抽取：只读输入与配置，返回内存对象；禁止控制台调试输出和中间文件。
- CLI：可以把返回对象展示到标准输出，但不保存实验报告。
- experiments：可以保存 `experiments/outputs/<stage>/<run-id>/result.json`，并按要求人工追加
  `analysis.md`。
- 正式持久化：只发生在专家确认之后，通过 Elasticsearch Repository 和
  `LocalSourceDocumentStore`；PDF 阻塞 I/O 由 `run_blocking()` 隔离。
- 可观测性：后续应使用结构化日志、Tracing 和 Metrics adapter，不得重新引入 `data/runs`
  文件作为工作流通信协议。

> **副作用边界：** 正式抽取只返回内存对象；只有专家确认后的独立入库图可以保存 PDF 与索引，实验目录不参与正式业务状态传递。

---

## 设计决策

- 纯领域规则、Schema 转换和图构建保持同步；所有网络、磁盘和长耗时编排公共边界均为异步。
- `StageResult` 取代校验文件，避免容器本地盘依赖并支持并发任务隔离。
- CLI 的 `asyncio.run()` 只出现于最外层，同一用例内部不得嵌套事件循环。
- `run_blocking()` 复用有界执行器并协作式轮询结果，以兼容限制线程唤醒机制的容器环境。
- 生产图只保留真实业务依赖：Core → Attribute；Clause、Abstract 在 prepare 后并发。所有
  MLLM 调用经共享的 `models.mllm.max_concurrent_requests` 门禁，默认上限为 3；部署方应以
  显存、KV Cache、TTFT 与吞吐观测调整该值。
- 独立入库图为 Prepare →（Text Embedding || Visual Embedding）→ Persist；它不接入生产提取
  主图。Persist 内先原子保存 PDF，再写 ES 并按 attempt ID 回读。

> **并发与事务边界：** 异步不等于无界并发；提取和入库两张图都保持真实业务依赖，并以共享配额或节点依赖限制外部调用。

---

## 验证方式

```bash
python -m pytest -q
rg -n "print\\(|write_text\\(|httpx\\.Client|from openai import OpenAI|\\.invoke\\(" \
  src/contract_processor/infrastructure/extraction \
  src/contract_processor/application
```

测试门禁覆盖整个 `src/contract_processor`：禁止调试 `print()`、同步网络客户端或同步图调用；
除明确的 `LocalSourceDocumentStore` 外禁止正式文件写入，并通过 AST 检查异步函数不直接调用
阻塞文件 API、公开同步函数不承担 I/O。

> **验证要求：** 生产运行时改动必须同时通过异步边界静态门禁和相关单元 / 集成测试，不能以实验脚本能够运行代替正式验证。
