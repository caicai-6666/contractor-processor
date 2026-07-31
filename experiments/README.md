# Experiments

本目录用于快速验证 PDF 渲染、MLLM 抽取、Embedding、Reranker 或工作流设计，不属于正式产品代码。

## 新建 Demo

每个 Demo 使用单独目录，并以简短、可读的名称命名：

```text
experiments/
├── pdf_render_demo/
│   ├── demo.py
│   └── README.md
└── outputs/                 # 运行结果，不提交 Git
```

可从 [`_template/README.md`](_template/README.md) 复制说明模板。

## 规则

- Demo 可直接调用本地 vLLM 或读取 `data/input/` 中的合同，但不得提交合同、密钥或运行输出。
- 图片、JSON、日志等结果写入 `experiments/outputs/<demo-name>/`。
- Demo 验证有效后，将可复用实现迁移至 `src/`，并在 `tests/` 中补充自动化测试；不要将实验代码直接作为正式模块依赖。
- 每个 Demo 的 README 应记录目的、输入、运行命令和结论，避免重复试验。
