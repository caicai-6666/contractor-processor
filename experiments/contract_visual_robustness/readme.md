# 变换合同视觉召回实验

> **定位：** 针对已入库原始合同生成派生 PDF，并仅以 `document_visual_vector` 执行 KNN 查询，观察视觉召回对受控变换的稳定性。

---

## 变换规则

- 多页合同：删除最后一页。
- 单页合同：顺时针旋转 90°并横向缩小 10%。

---

## 运行

```bash
python experiments/contract_visual_robustness/run.py --mock-run experiments/outputs/contract_ingestion_mock/20260802T132616923404Z
```

---

## 输出与边界

每次输出的 `report.md` 按查询合同展示全部候选的降序 ES KNN 得分和原合同排名；`transformed/` 保存派生 PDF。实验不写入 Elasticsearch。

> **解释边界：** 该实验只观测指定变换下的视觉召回，不替代合同近重复的最终 VL 精判。
