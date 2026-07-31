# Attribute 字段目录

> 状态：当前为空，等待首轮合同批次完成解析、字段归并与人工审核后统一更新。  
> 适用范围：合同中发现、但尚未进入 Core 的动态元数据字段。  
> 规范地位：[`attribute.yaml`](attribute.yaml) 是程序读取与提示词注入的唯一规范源；本文件记录使用规则、迭代说明与审核入口。

## 1. 使用规则

- Attribute 是尚未被专家提升为 Core 的字段，不得直接写入 Core 字段库。
- 每一轮固定合同批次处理结束后，统一完成字段归并、统计与审核，再更新 `attribute.yaml`；处理单份合同时不直接修改该文件。
- 新字段必须先与 Core 和已有 Attribute 进行向量召回及大模型语义判定。仅当其与已有字段不一致且具有可定义的业务含义时，才新增为 Attribute。
- Attribute 的频次以“出现的不同合同数”为主要统计口径；同一合同中同一字段的重复出现只计一次。
- 专家决定升级字段时，应将字段定义迁移至 `../core/core.yaml`，并在本文件及字段变更记录中保留迁移理由和时间。

## 2. 字段结构

每个 Attribute 复用 Core 的字段定义结构，并额外记录统计及审核信息：

```yaml
field_id: ""
name: ""
meaning: ""
aliases: []
not_meaning: []
output:
  type: ""
  format: null
  nullable: true
  example: null
extraction_rule: ""
examples: []
statistics:
  occurrence_count: 0       # 总发现次数
  contract_count: 0         # 出现的不同合同数，专家决策的主排序指标
  first_seen_round: null
  last_seen_round: null
  source_contract_ids: []
review:
  status: pending           # pending | approved_core | rejected | archived
  decision_reason: null
  reviewed_at: null
```

## 3. 当前字段

当前没有已归并的 Attribute。首轮合同批次处理完毕后，应按 `statistics.contract_count` 降序将字段写入 `attribute.yaml`，并同步补充本节的审核摘要。

| field_id | 名称 | 不同合同数 | 审核状态 |
| --- | --- | ---: | --- |
| 暂无 | 暂无 | 0 | 等待首轮迭代 |
