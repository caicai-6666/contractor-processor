# Contract Processor

用于从 PDF 合同中提取 Core 元数据并发现动态 Attribute 的本地工作流。

开发前请先阅读 [项目说明](description/PROJECT.md) 与[架构设计](description/architecture/PROJECT_STRUCTURE.md)。

## 初始化后的可用命令

```bash
PYTHONPATH=src python3 -m contract_processor --help
PYTHONPATH=src python3 -m contract_processor inspect-fields
```

完整工作流尚未实现；`process-batch` 是后续 LangGraph 工作流的稳定 CLI 入口。
