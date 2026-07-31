# Clause 分阶段提取实验

本实验与 `core_field_extraction` 分别调用模型，不共享任务地图，只共享稳定的“system message + 公共规则 + 整份 PDF 图像”前缀，避免 Core 事实污染 Clause，同时利用 vLLM prefix caching。

## 实验流程

```text
整份合同 PDF
  → Step 1：从上到下穷举结构候选
      └── 标题、编号、标签、分点；不做 Clause 资格筛选
  → 程序归一化：解析明显的项目符号和“标签：”结构
  → Step 1B：全局核销原子，形成互不重叠的完整条款组
      └── 只输出连续索引组、标题策略和忽略索引
  → 程序投影：父标题 + 自身标题融合，并生成首尾硬边界
  → Step 2：遍历条款组，逐组独立判断 include/exclude
  → 程序解析标题来源与层级编号
  → Step 3：按复核单元逐条抽取完整原文
  → 程序执行重复、包含和编号冲突硬校验
  → 按原文顺序合并为 final_clauses.json
```

Step 1 的 `candidates` 是结构原子，不代表条款资格，也不直接作为抽取单元。程序会从 `opening_anchor` 中确定性解析明显的 `◆`、`（1）` 和“付款方式：”等原文结构，但不根据正文语义拟造标题。若模型把父标题单独列为一项，程序只在“下一项明确带分点标记”或“标题以冒号结束并紧邻正文”时向后传播父标题。

Step 1B 读取带一基索引的完整 Step 1 结果，只能用索引表达连续分组。所有原子必须恰好进入一个组或忽略列表；标题空壳与紧随正文合组，父章节与叶子条款不能同时保留，无自有标题的内部列举留在最近完整条款组。程序校验全量核销、有序、连续和非重叠后，确定性生成 `source_candidate_indices/opening_anchor/end_before_anchor`。下一原子的开头就是当前组的硬结束边界。

Step 2 不接收可重写的合同地图，而是遍历条款组的 `candidate_index/fused_heading/location` 只读投影。每次请求只输出 `reason` 和 `decision`，不能修正、拆分、合并、补充或复制结构。保留决定映射回完整条款组；若 Step 1 漏项，应修复 Step 1，若条款边界不对，应修复 Step 1B。

## 标题与编号

- `（n）标签：正文` 中的标签是子项自有标题，自有标题优先于父标题。
- 子项只有编号或项目符号时，可以继承最近的原文明示父标题；自有与父标题均不存在时排除。
- 自有标题必须出现在最终 `source_text`；校验时只清除 `heading` 比较形式中的 Unicode 空白，再使用允许标题字符之间出现空白的正则匹配原始 `source_text`，后者从不修改。继承标题不必在子项正文中重复，程序用内部 `heading_source` 区分校验方式。
- 父编号和编号型子标记可以由程序组合为“四（1）”等引用字符串；项目符号不会被伪造成编号。
- 原文开头的“七、”若与已有父编号“七”相同，视为同一个顶层编号而非子标记；编号组合函数也会执行同级去重，避免生成“七七、”。
- `2.1` 这类完整层级号直接保留；数字父编号 `7.` 与子标记 `1)` 组合为 `7.1`，不再生成 `71.`。
- 质保、售后、维修、技术支持、培训和响应时限是两次地图检查的重点内容，但不会因此改变原文顺序。

## 全 PDF 与前缀布局

四类模型请求都携带整份 PDF，并采用：

```text
相同 system message + 共享公共规则 + 全部 PDF 页面图像 + 当前任务后缀
```

`--max-pages` 是安全上限，不是截断数量。PDF 超过上限时实验会在模型调用前失败；提高它前需同步确认 vLLM 的 `--limit-mm-per-prompt` 和上下文容量。

## 运行

```bash
python experiments/clause_extraction/run.py \
  --pdf "data/input/example.pdf" \
  --max-pages 5
```

可选参数包括 `--output-dir`、`--max-pages`、`--max-model-len` 和 `--print-prompts`。IDE 直接运行时可修改 `run.py` 底部的 `DEFAULT_PDF_PATH`。

## 主要产物

每次运行写入 `experiments/outputs/clause_extraction/<UTC 时间戳>/`：

- `run_manifest.json`、`00_common_prefix_prompt.txt`：运行与公共前缀快照；
- `01_structure_map.json`：模型原始结构穷举结果；
- `01_normalized_structure_map.json`、`01_structure_normalization.json`：程序归一化地图及变更记录；
- `01b_boundary_plan.json`、`01b_boundary_validation.json`：索引边界计划及全量非重叠校验；
- `01b_clause_groups.json`：程序确定性生成的完整条款组与首尾锚点；
- `02_review_candidates.json`：程序生成的融合标题与位置列表；
- `02_reviews/<序号>/`：当前候选、原始响应、指标、去留决定或失败记录；
- `02_review_manifest.json`：逐候选成功率、保留/排除数量和聚合 token；
- `02_clause_review.json`：全部逐候选判断的汇总；
- `02_clause_units.json`：程序解析标题来源与编号后的 Step 3 单元；
- `03_units/<序号>/`：当前单元、原始响应、指标、结果或失败记录；
- `03_unit_manifest.json`：单元成功率和聚合 token 指标；
- `03_clause_extraction.json`、`final_clauses.json`：按复核顺序合并的最终结果；
- `clause_validation.json`：失败单元、完整对象重复、原文重复/包含和编号重复检查；
- `metrics.json`：Step 1、Step 1B、Step 2 和 Step 3 指标。

单个 Step 2 候选或 Step 3 单元失败时只隔离当前项，并继续后续处理。`clause_validation.json.is_complete` 反映是否存在调用失败；`is_valid` 进一步纳入完整对象重复、忽略空白后的原文重复、原文包含和非空编号重复。校验只比较规范化副本，不修改最终 `source_text`。

## 最终结构

最终 Clause 遵循 [`clause.yaml`](../../description/fields/clause/clause.yaml) 0.4，仍只有五个字段：

```yaml
clauses:
  - clause_number: "四（1）"
    heading: 甲方责任与义务
    category: rights_and_obligations
    source_text: |
      （1）甲方应……
    page_refs: [2]
```

不生成 `clause_id`、`sequence`、摘要、`reason`、`status` 或 `confidence`。
