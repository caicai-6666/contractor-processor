# 终审待入库 Mock 包络实验

> **定位：** 本实验批量调用正式 `run_single_file()` 返回接口，对每份 PDF 执行 production 提取并补充最小审核追溯信息，生成供后续入库模块使用的完整确认包络。

> **副作用边界：** 实验只在 `experiments/outputs/` 写入 JSON；不写 Elasticsearch，也不复制或修改源 PDF。

---

## 使用方式

```bash
python experiments/contract_ingestion_mock/run.py \
  --input-dir data/input \
  --reviewer mock-reviewer \
  --comment "Mock 审核通过，用于入库模块开发"
```

如需固定审核时间，可传入带时区时间：

```bash
python experiments/contract_ingestion_mock/run.py \
  --reviewed-at 2026-08-02T18:30:00+08:00
```

---

## 输出包络

输出目录为：

```text
experiments/outputs/contract_ingestion_mock/<run-id>/
├── manifest.json
├── 01_ingestion_package.json
├── 02_ingestion_package.json
└── ...
```

`manifest.json` 保存源 PDF 相对路径、`document_id`、包络文件名和逐份状态。每个成功包络均
经过正式 `ContractReviewConfirmation` 校验，结构为：

```json
{
  "document_id": "<PDF SHA-256>",
  "review": {
    "reviewer": "mock-reviewer",
    "reviewed_at": "2026-08-02T18:30:00+08:00",
    "comment": "Mock 审核通过，用于入库模块开发"
  },
  "result": {
    "document_id": "<相同 PDF SHA-256>",
    "source_name": "example.pdf",
    "core": {},
    "attribute": [],
    "clause": [],
    "abstract": {},
    "processing": {}
  }
}
```

> **契约门禁：** 每个成功包络均经过正式 `ContractReviewConfirmation` 校验；内外层 `document_id` 必须对应同一 PDF SHA-256。

---

## 批处理与失败处理

脚本递归查找输入目录中的 `.pdf`，逐份串行处理。单份失败不会阻止后续文件；失败信息写入
manifest，阶段校验失败还会保存不含模型 raw response 的诊断 JSON。只要存在失败，脚本最终
返回非零退出码，但已经成功生成的包络保持可用。

> **诊断边界：** 失败诊断不保存模型 raw response；成功包络与失败记录可在同一运行中并存。

---

## 非目标与分析

该产物是研发 Mock，不代表已经实现正式入库、PDF 存储、空值裁剪、视觉向量或重复版本替换。
后续若要求分析本次运行，必须在对应 `<run-id>/analysis.md` 中按实验分析模板追加记录。

> **解释边界：** Mock 包络只验证输入契约与生产提取结果的衔接，不能作为正式入库能力已完成的证据。
