# CLI 入口

> **适用范围：** CLI 是正式应用用例的命令行适配层，负责参数、调用和控制台响应；它不承载业务抽取算法或实验落盘。

---

## 模块用途

`src/contract_processor/interfaces/cli/` 是正式应用用例的命令行适配层，不承载抽取算法，也不
生成实验报告。每个入口以同步 `main()` 满足 console script 协议，实际工作由
`async_main()` 异步执行。

---

## 当前入口

| 模块 | 职责 | 主要参数 |
| --- | --- | --- |
| `inspect_fields` | 异步检查 Core/Attribute 机器规范 | `--project-root` |
| `run_single_file` | 按模式异步处理一份 PDF 并向标准输出返回 JSON | `--mode`、`--pdf`、`--project-root` |
| `run_batch` | 按模式逐份等待异步工作流并输出批次摘要 | `--mode`、`--input-dir`、`--project-root` |

```bash
python -m contract_processor.interfaces.cli.inspect_fields
python -m contract_processor.interfaces.cli.run_single_file \
  --mode production --pdf data/input/example.pdf
python -m contract_processor.interfaces.cli.run_batch --mode production
```

安装后对应 `contract-inspect-fields`、`contract-run-single` 和 `contract-run-batch`。

> **使用边界：** 控制台 JSON 是接口响应；需要保存结果、报告或 `analysis.md` 时，必须使用相应实验入口而不是扩展正式 CLI 的文件副作用。

---

## 关键实现与设计决策

- CLI 只解析参数、组装用例并展示结果；Core/Clause/Attribute/Abstract 逻辑均在正式服务中。
- `run_single_file.run_single_file()` 是供本地适配器与实验复用的异步函数，返回
  `ContractProcessingResult` 或 discovery 专用结果，不打印也不写文件；`async_main()` 将其
  序列化到标准输出并同时返回同一对象，同步 `main()` 只负责事件循环和退出码。
- `--mode` 只接受 `production` 或 `discovery`；省略时读取 `settings.runtime.mode`。
- 当前未提供具体 `FieldDiscoveryService`，因此 discovery 会在 PDF 渲染和模型连接前明确失败；
  这是能力未实现的门禁，不代表合法的“零候选”结果。
- CLI 不创建 `data/runs`，不写 `contract_result.json`，也不承担历史结果恢复或实验验收。
- 批量入口默认串行等待每份异步任务，防止对单个本地 vLLM 产生无界并发；部署侧可在有资源
  配额和任务队列后增加受控并发。
- 需要保存调试结果、运行报告或 `analysis.md` 的操作属于 `experiments/`。
- FastAPI 应直接调用 application 用例，不得启动 CLI 子进程或导入 CLI 模块。
- `contract_ingestion_mock` 是实验边界，可调用上述返回函数批量构造终审包络；正式 FastAPI
  和正式入库用例仍必须直接依赖 application 端口，不能依赖 CLI。

> **模式与并发边界：** 未实现的 discovery 必须在昂贵资源初始化前 fail closed；批量 CLI 默认串行，不能因异步函数而形成无界模型并发。

---

## 依赖与注意事项

单文件和批量处理依赖 PDF、配置及 vLLM；字段检查不调用模型。控制台输出属于接口响应，
与正式算法内部禁止调试 `print()` 是不同边界。

> **接口边界：** FastAPI 应直接调用 application 用例，不能启动 CLI 子进程或以 CLI 模块替代服务接口。
