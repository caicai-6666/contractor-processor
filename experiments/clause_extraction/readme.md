# Clause 提取实验入口

本目录复用正式异步 Clause 算法，验证结构发现、边界归并、候选复核、逐单元抽取及最终
重复/包含校验。算法和 Prompt 位于
`src/contract_processor/infrastructure/extraction/clause/`。

## 运行

```bash
python experiments/clause_extraction/run.py --pdf data/input/example.pdf
```

可使用 `--output-dir` 修改实验输出根目录。PDF 页数超过正式配置上限时会在模型调用前失败，
不会静默截断。

## 结果与边界

通过正式阶段门禁的 payload 保存为：

```text
experiments/outputs/clause_extraction/<UTC-run-id>/result.json
```

正式服务不生成运行目录、中间 Prompt、raw response 或指标文件。新诊断需求应先在本实验
目录实现和验证，迁入正式代码时去掉控制台调试与报告写入。人工分析按模板追加
`analysis.md`。
