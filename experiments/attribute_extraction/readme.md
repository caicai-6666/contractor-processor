# Attribute 空节点实验

> **定位：** 本实验不调用模型，而是直接复用正式 `EmptyAttributeExtractionService`，验证空 Attribute 目录的明确运行语义。

---

## 验证条件

当字段目录为 `status: empty` 且 `fields: []` 时，主工作流应稳定生成空候选数组。

> **门禁：** 空模式、文档身份、规范版本和候选数量均由正式服务在内存校验；任一条件不通过就不返回结果。

---

## 运行

```bash
python experiments/attribute_extraction/run.py --pdf data/input/example.pdf
```

---

## 输出与边界

每次运行在 `experiments/outputs/attribute_extraction/<run_id>/result.json` 保存当前业务结果 `[]`。

字段目录一旦不再为空，实验和正式节点都会立即失败，避免把尚未接入的 Attribute
实现误报为空结果。

> **迁移边界：** 当 Attribute 目录开始承载字段定义时，应使用正式 Attribute 提取服务；本实验不应继续作为非空目录的回退路径。
