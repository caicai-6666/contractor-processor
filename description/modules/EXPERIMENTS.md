# 快速实验区

`experiments/` 是个人开发阶段的快速 Demo 工作区，用于立刻验证模型调用、PDF 处理或检索效果。

实验代码不属于正式模块，不得被 `src/` 依赖。验证成功且具有复用价值时，应迁移至 `src/`，并为稳定行为补充 `tests/`。详细规则与模板见 [`../../experiments/README.md`](../../experiments/README.md)。

当前实验：

- [Core 字段提取实验](CORE_EXTRACTION_EXPERIMENT.md)
- [Clause 三阶段提取实验](CLAUSE_EXTRACTION_EXPERIMENT.md)
