# Contract Processor

> 从 PDF 合同中发现业务字段，并按固定 Core、Attribute Schema 提取字段、Clause 与摘要的本地工作流。

项目具备字段发现与正式生产两套显式拓扑：production 执行固定 Attribute 提取；discovery 的具体 `FieldDiscoveryService` 尚未注入，因此会在渲染 PDF 和连接模型前明确失败。当前 Attribute 目录为 `0.3/draft`，包含 10 个专家预置字段。

---

## 阅读路径

| 目标 | 建议先读 |
| --- | --- |
| 了解项目范围、术语与文档导航 | [项目说明](description/PROJECT.md) 与 [文档导航](description/README.md) |
| 理解 production / discovery 边界 | [Attribute 双运行模式设计](description/architecture/attribute-operating-modes.md) |
| 查阅代码目录与模块关系 | [项目结构](description/architecture/project-structure.md) |
| 本地配置、运行或排障 | [运行维护文档](description/README.md#运行维护与实验) |

---

## 安装与基础检查

```bash
python3 -m pip install -e ".[dev]"
python -m contract_processor.interfaces.cli.inspect_fields
```

> **检查结果：** 当前目录应显示 12 个 Core 字段与 10 个固定 Attribute 字段。

---

## CLI 测试入口

单文件测试统一从 `interfaces/cli` 运行：

```bash
python -m contract_processor.interfaces.cli.run_single_file \
  --mode production \
  --pdf "data/input/example.pdf"
```

也可在 IDE 中打开
[`run_single_file.py`](src/contract_processor/interfaces/cli/run_single_file.py)，修改
`DEFAULT_PDF_PATH` 后按模块运行。届时入口通过异步 LangGraph 工作流在控制台返回：

- `core`：十二个 Core 字段的状态、证据与规范值；
- `attribute`：按当前 10 个固定 Attribute 定义逐字段返回状态、证据与规范值；
- `clause`：按原文顺序保存的条款；
- `abstract`：固定六栏目结构和固定渲染摘要正文。

`--mode` 可取 `production` 或 `discovery`，省略时读取 `configs/settings.yaml` 的
`runtime.mode`。

> **运行门禁：** production 会执行非空 Attribute 目录的固定字段提取；discovery 未注入具体服务时必须明确失败。两种模式都不得用空数组伪装处理成功。

---

## 结果、批处理与实验边界

正式抽取只返回内存候选，不写 `data/runs`、Prompt、raw response 或校验报告。正式代码已经
按 FastAPI 接口层组织并冻结前后端 DTO；当前需求不创建路由。Elasticsearch mapping 的
异步 Factory 以 Core YAML 为唯一来源，正式写入仍只能发生在专家确认之后。

批量入口逐份等待异步任务，并把摘要输出到控制台：

```bash
python -u -m contract_processor.interfaces.cli.run_batch --mode production
```

> **产物边界：** 需要保存调试结果或实验报告时使用 [`experiments/`](experiments/readme.md)；正式 `interfaces/cli/` 不落盘。自动化单元测试维护在 `tests/`。

生产异步与副作用边界见[异步生产运行时](description/operations/async-production-runtime.md)。
