# Experiments

> 本目录用于快速验证 PDF 渲染、MLLM 抽取、Embedding、Reranker 或工作流设计，不承载正式业务实现。

Core、Clause、Contract Summary 和终审入库均已迁入 `src/contract_processor/`；它们在本
目录中的 `run.py` 只是调用正式算法的验收入口。Attribute 空实验与主工作流共用同一个空
服务。各入口见：

- [`core_field_extraction`](core_field_extraction/readme.md)
- [`clause_extraction`](clause_extraction/readme.md)
- [`contract_summary_generation`](contract_summary_generation/readme.md)
- [`attribute_extraction`](attribute_extraction/readme.md)
- [`contract_processing_batch`](contract_processing_batch/readme.md)
- [`contract_ingestion_mock`](contract_ingestion_mock/readme.md)
- [`contract_ingestion_persistence`](contract_ingestion_persistence/readme.md)
- [`contract_visual_retrieval`](contract_visual_retrieval/readme.md)
- [`contract_visual_robustness`](contract_visual_robustness/readme.md)
- [`field_discovery_stage_one`](field_discovery_stage_one/readme.md)：字段发现第一大步统一流水线
- [`field_discovery_group_consolidation`](field_discovery_group_consolidation/readme.md)：历史候选池
  的组级收敛复现入口；正常流程无需单独运行

三个模型算法共享完全一致的 system、PDF 公共规则、页面图像和页面可见范围，但不合并
业务输出。正式边界见[正式抽取服务](../description/capabilities/extraction-services.md)，前缀分层
与验收标准见 [MLLM Prompt 共享前缀设计](../description/architecture/mllm-prompt-prefix.md)。

> **依赖方向：** 正式模块不得依赖实验目录；已有薄入口只能沿 `experiments → src` 的方向调用。

---

## 新建 Demo

每个 Demo 使用单独目录，并以简短、可读的名称命名：

```text
experiments/
├── pdf_render_demo/
│   ├── demo.py
│   └── readme.md
└── outputs/                 # 运行结果，不提交 Git
```

可从 [`_template/readme.md`](_template/readme.md) 复制说明模板。

---

## 规则

- Demo 可直接调用本地 vLLM 或读取 `data/input/` 中的合同，但不得提交合同、密钥或运行输出。
- 图片、JSON、日志等结果写入 `experiments/outputs/<demo-name>/`。
- Demo 验证有效后，将可复用实现迁移至 `src/`，并在 `tests/` 中补充自动化测试；正式
  模块不得依赖实验目录，已有薄入口只能沿 `experiments → src` 方向调用。
- 每个 Demo 的 README 应记录目的、输入、运行命令和结论，避免重复试验。
- 新建可执行实验时，应在 `if __name__ == "__main__"` 中提供集中、可编辑的 IDE 默认配置；
  实际命令行参数必须优先于该编辑区，保证 IDE 与 CLI 使用同一套参数解析和执行路径。

> **产物与分析：** 运行结果写入 `experiments/outputs/<demo-name>/`，不提交 Git。需要人工分析时，按 [实验分析模板](experiment-analysis-template.md) 追加到对应运行目录的 `analysis.md`。
