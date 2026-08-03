# Abstract 生成实验入口

> **定位：** 本目录调用正式异步 Abstract 算法。模型直接读取完整 PDF，生成固定六栏目候选；程序验证证据、页码和栏目边界，并只对失败栏目执行局部重试。

---

## 运行

```bash
python experiments/contract_summary_generation/run.py --pdf data/input/example.pdf
```

可使用 `--output-dir` 修改实验输出根目录。模型和摘要策略读取 `configs/settings.yaml` 与
`data/definitions/contract_summary.yaml`。

> **配置来源：** 模型与摘要策略由 `configs/settings.yaml` 和机器定义共同决定，实验命令不单独复制这些业务规则。

---

## 结果与边界

通过最终业务校验的 payload 保存为：

```text
experiments/outputs/contract_summary_generation/<UTC-run-id>/result.json
```

正式算法中的重试反馈、validation 和 metrics 均在内存中传递。实验若要观察更细粒度信息，
应在实验包装层实现采集，不得让正式算法依赖报告文件。人工分析按模板追加 `analysis.md`。

> **产物边界：** 正式算法的重试、validation 与 metrics 只在内存传递；实验需要的额外诊断只能由包装层显式采集。
