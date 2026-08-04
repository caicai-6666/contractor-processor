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
| `run_batch` | 按模式逐份等待异步工作流并输出批次摘要 | `--mode`、`--input-dir`、`--project-root`、`--max-documents` |

```bash
python -m contract_processor.interfaces.cli.inspect_fields
python -m contract_processor.interfaces.cli.run_single_file \
  --mode production --pdf data/input/example.pdf
python -m contract_processor.interfaces.cli.run_batch --mode production
```

安装后对应 `contract-inspect-fields`、`contract-run-single` 和 `contract-run-batch`。

> **使用边界：** 控制台 JSON 是接口响应；批量 discovery 另会保存固定格式的字段审核 YAML。调试报告或 `analysis.md` 仍必须使用相应实验入口。

---

## 关键实现与设计决策

- CLI 只解析参数、组装用例并展示结果；Core/Clause/Attribute/Abstract 逻辑均在正式服务中。
- `run_single_file.run_single_file()` 是供本地适配器与实验复用的异步函数，返回
  `ContractProcessingResult` 或 discovery 专用结果，不打印也不写文件；`async_main()` 将其
  序列化到标准输出并同时返回同一对象，同步 `main()` 只负责事件循环和退出码。
- `--mode` 只接受 `production` 或 `discovery`；省略时读取 `settings.runtime.mode`。
- `run_batch --mode discovery` 使用正式的字段发现批次父图：第一阶段为每份合同执行
  PDF Prepare → Core → Attribute → 候选发现与准入 → 向量归并 → 关系图分组 → 组级收敛 →
  全局门禁；第二阶段只将收敛后的冻结字段分别回扫每份不同合同，并返回字段—合同观察及频率
  统计。完成后会将待审核定义和统计写入 `data/definitions/discovery/result/<batch_id>.yaml`，但不会
  修改正式字段目录。
- CLI 不创建 `data/runs`，不写 `contract_result.json`，也不承担历史结果恢复或实验验收；正式
  discovery 用例自身负责按批次原子保存字段审核 YAML。
- 批量入口的文档级处理默认串行，防止对单个本地 vLLM 产生无界并发；单合同中的候选语义
  准入和第二阶段单字段回扫共享各自批次级请求限流器进行受控并发。
- 直接在 IDE 运行 `run_batch.py` 且未配置参数时，模块末尾的 `IDE_DEFAULT_ARGS` 默认运行
  `discovery` 和 `data/input` 下全部 PDF；加入 `--max-documents 1` 才只运行排序后的首份。
  只要 IDE 或终端传入任意参数，默认值便不会生效。
- Discovery 进度写入标准错误：逐合同只展示模型候选、准入和拒绝计数，收敛阶段展示身份、
  分组、冻结字段和失败分组，第二阶段展示任务成功/失败数，最后展示 YAML 路径；完整 JSON 仍只写标准输出。
- JSON 顶层包含 `batch_id`、起止时间与 `processing`；其中绑定 MLLM/Prompt、Embedding
  模型/维度/字段摘要指令版本、Discovery 目录版本及候选预算，便于复现实验与比较批次。
- 需要保存调试结果、运行报告或 `analysis.md` 的操作属于 `experiments/`。
- FastAPI 应直接调用 application 用例，不得启动 CLI 子进程或导入 CLI 模块。
- `contract_ingestion_mock` 是实验边界，可调用上述返回函数批量构造终审包络；正式 FastAPI
  和正式入库用例仍必须直接依赖 application 端口，不能依赖 CLI。

> **模式与并发边界：** 第一阶段按合同串行，第二阶段按字段—合同任务动态并发；所有 MLLM 调用都必须经过受配置约束的请求限流器。

---

## 依赖与注意事项

单文件和批量处理依赖 PDF、配置及 vLLM；字段检查不调用模型。控制台输出属于接口响应，
与正式算法内部禁止调试 `print()` 是不同边界。

> **接口边界：** FastAPI 应直接调用 application 用例，不能启动 CLI 子进程或以 CLI 模块替代服务接口。
