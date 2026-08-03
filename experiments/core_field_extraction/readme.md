# Core 字段提取实验入口

本目录只负责调用正式异步 Core 算法并保存一次实验结果。算法和 Prompt 分别位于
`src/contract_processor/infrastructure/extraction/core/` 及其 `prompts/`；正式源码不会读取
或写入本实验目录。

## 运行

```bash
python experiments/core_field_extraction/run.py --pdf data/input/example.pdf
```

可使用 `--output-dir` 修改实验输出根目录，或在 IDE 中调整 `DEFAULT_PDF_PATH`。模型、采样、
超时和视觉页数读取 `configs/settings.yaml`。

## 结果

入口将通过正式阶段门禁的最终 payload 写入：

```text
experiments/outputs/core_field_extraction/<UTC-run-id>/result.json
```

正式算法内部的 Prompt、raw response、metrics 和 validation 只在内存中传递。若新实验需要
额外诊断材料，应在本目录扩展实验代码，不得把写文件或调试输出加入正式 pipeline。

人工分析某次运行时，按 `experiments/experiment-analysis-template.md` 在同一运行目录追加
`analysis.md`；运行器不会自动生成该文件。
