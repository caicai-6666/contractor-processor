# 合同文档身份协议

> 状态：已确定；Core、Clause、Contract Summary 及后续持久化必须统一采用。

---

## 1. 身份定义

`document_id` 是原始 PDF 文件完整字节流的 SHA-256，小写十六进制表示，固定为 64 个字符且不添加业务前缀：

```text
document_id = lowercase_hex(SHA-256(original_pdf_bytes))
```

哈希在程序读取原始文件时流式计算，不基于文件名、渲染图片、OCR 文本、合同编号、合同标题或模型输出。模型不生成、不复述也不校验 `document_id`。

> **唯一来源：** `document_id` 只由原始 PDF 文件字节确定；任何业务字段、文本派生物或模型输出都不能参与计算或替代它。

---

## 2. 与合同编号的边界

`contract_number` 是可空 Core 业务字段和摘要展示栏目，不再承担文档身份职责：

- 原文明确存在唯一当前合同编号时正常提取；
- 没有编号时返回 `not_found + null`；
- 归属不明或存在多个冲突编号时返回 `ambiguous/conflicting + null`；
- 编号缺失、重复或冲突不阻断 Core、Clause、摘要和索引流程；
- 精确编号检索可以使用 `contract_number` 过滤，但不能对其施加全库唯一约束。

SHA-256 不是合同编号缺失时的“回退值”，而是所有文档无条件使用的主身份。合同编号不得被写入 `document_id`，哈希也不得被写入 `contract_number`。

> **身份与业务字段分离：** `contract_number` 便于展示和检索，但可以缺失、重复或冲突；这些情况不得阻断合同级产物或索引。

---

## 3. 身份语义

`document_id` 表示文件字节身份，不表示法律或业务上的“同一合同”：

- 完全相同的 PDF 字节得到相同 ID，可用于拒绝重复导入或幂等处理；
- PDF 重新签章、重新导出、修改元数据或发生任意字节变化时得到新 ID，即使可见正文相同；
- 合同扫描件、电子原件和后续版本应保留不同 ID；它们是否属于同一业务合同，由
  “哈希精确判重、VL-Embedding 召回、VL 模型精判、专家确认”的独立入库流程判断；
- 不对 PDF 做规范化后再计算哈希，避免不同原始文件被不可逆地折叠。

业务合同级重复判断、可选的 `contract_id` 关系以及旧版本安全替换见
[合同终审入库与多模态判重设计](contract-ingestion-deduplication.md)。该流程不改变
`document_id` 的文件字节身份语义。

> **文件版本不等于业务合同：** 文件发生任意字节变化就应产生新的 `document_id`；是否属于同一业务合同必须进入独立判重与专家确认流程。

---

## 4. 输出协议

### 4.1 合同级产物

三个合同级实验最终产物均在根节点携带同一个 `document_id`：

```json
{
  "document_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "fields_or_sections": {}
}
```

具体字段名由模块决定：Core 使用 `fields`，Clause 使用 `clauses`，Contract Summary 使用 `sections` 和 `text`。

### 4.2 运行 manifest

`run_manifest.json` 同时记录：

```json
{
  "document_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "document_id_algorithm": "sha256",
  "document_id_source": "original_pdf_bytes"
}
```

持久化层对 `document_id` 建立唯一约束。Clause 项如需内部 ID，可由 `document_id + 数组下标` 确定性生成；合同向量记录的主键直接使用 `document_id`。

> **跨产物一致性：** 同一原始 PDF 的所有合同级产物、运行 manifest 与持久化主键必须携带完全相同的 `document_id`。

---

## 5. 实现与校验

### 5.1 实现要求

统一实现位于 `infrastructure/pdf/document_identity.py`，领域层只接受符合 `^[0-9a-f]{64}$` 的文档 ID。哈希必须在模型调用和 PDF 渲染前计算，以便所有下游产物从入口开始携带稳定身份。

### 5.2 验收要求

验收至少覆盖：

1. 同一文件重复计算得到相同 ID；
2. 任意字节变化导致 ID 变化；
3. 三个实验对同一输入写出完全相同的 ID；
4. 合同编号为空时仍能生成最终 Core、Clause 和摘要产物；
5. 模型 Schema 和 Prompt 不要求模型输出 `document_id`；
6. 最终产物拒绝非 64 位小写 SHA-256 格式的 ID。

---

## 6. 历史产物迁移

旧实验产物可能把合同编号写入 Core `document_id`、在摘要中使用 `contract_id`，或在 Clause 根节点没有身份字段。这些产物作为审计记录保持原样，不得批量覆盖：

- 新消费者必须根据 Core `1.1`、Clause `0.5`、Contract Summary `0.4` 或更高版本识别新身份协议；
- 历史产物需要进入新工作流时，必须重新读取 manifest 指向的原始 PDF 并计算 SHA-256；
- 原始 PDF 已不存在时，不能根据旧合同编号、文件名、摘要文本或渲染图片伪造新 `document_id`；
- 重新生成的产物写入新的运行目录，并保留与历史 run ID 的迁移关系。

> **迁移禁止项：** 原始 PDF 缺失时，不能依据合同编号、文件名、摘要文本或渲染图片伪造新的 `document_id`。
