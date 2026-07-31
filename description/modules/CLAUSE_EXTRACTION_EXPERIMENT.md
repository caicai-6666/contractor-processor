# Clause 分阶段提取实验

## 模块用途

[`experiments/clause_extraction`](../../experiments/clause_extraction) 用于验证“结构穷举、全局边界整理、逐组筛选、逐条抽取”的多模态 Clause 工作流。它与 Core 实验完全隔离，不被正式 `src/` 模块依赖。

## 主要职责

1. 将整份 PDF 的所有页面渲染为内存 PNG；
2. Step 1 按视觉顺序穷举标题、编号、标签和分点，不提前施加条款资格；
3. 程序从锚点中确定性解析显式项目符号和“标签：”结构，并保留归一化审计产物；
4. Step 1B 只用索引把结构原子整理为连续、有序、互不重叠的完整条款组，并显式核销忽略项；
5. 程序校验全量核销并生成每组的开始锚点、下一原子硬结束锚点，再投影为只含融合标题与位置的紧凑列表；
6. Step 2 遍历列表，每次只对一个完整条款组输出 `reason + include/exclude`，不重写边界；
7. 程序将保留决定映射回条款组，按“自有标题优先、否则继承最近父标题”解析提取单元，并组合原文层级编号；
8. Step 3 每次处理一个复核单元，只抽取硬边界内的完整 Clause 原文；
9. 保存提示词、Schema、原始响应、指标、失败单元和重复/包含校验产物。

## 设计决策

首次视觉扫描只承担结构召回，避免模型同时执行“找全”和“筛准”造成非标准合同的系统性漏项。由于结构原子可能把标题与正文拆开，或同时给出父章节和子条款，Step 1 结果不能直接逐项筛选。Step 1B 因此查看全量索引列表，用 `source_candidate_indices + heading_strategy + heading_candidate_index` 表达完整边界；模型不能复制或重写结构事实。程序硬校验每个索引恰好出现一次、组内连续、组间有序且不重叠。

程序从组首项取得标题与开始锚点，从组内原子合并页码，并把紧随组后的原子开头设为 `end_before_anchor`。Step 2 只读取 `candidate_index/fused_heading/location`；位置包含来源原子和首尾硬边界。模型逐组只判断去留，不返回候选结构。融合标题为 `null` 时程序禁止保留。结构漏项在 Step 1 修复，边界错误在 Step 1B 修复，资格筛选不承担补漏、拆分或合并。

模型漏填但锚点中明确存在的项目符号和“标签：”由程序确定性补齐。模型把父标题单列时，只有下一项明确带分点标记，或当前标题以冒号结束并紧邻正文，程序才传播父标题。该归一化只读取相邻结构和原文标点，不根据语义创造标题，并同时保存修改前后结构用于审计。

显式标记归一化会先判断开头标记是否与已有父编号重复；“parent=七、开头=七、”保持为单个顶层编号，不能写成子标记。候选已有“（1）”等子标记时，开头父编号也不能用于解析 `item_heading`。编号组合函数提供第二层同级去重保护，并分别处理完整小节号 `2.1`、阿拉伯数字父子号 `7.1` 和中文括号子号。

子项有原文标签时使用自有标题；只有编号或项目符号时继承最近父标题。内部 `heading_source` 仅控制业务校验，不进入最终五字段结果：自有标题必须出现在 `source_text`，程序只清除 `heading` 比较形式中的 Unicode 空白，并以允许字符间空白的正则匹配未经修改的原文；继承标题不要求在子项正文中重复。所有单元还必须包含 `opening_anchor` 且不得包含 `end_before_anchor`，从而在抽取阶段阻止错位和吞入下一条款。

最终校验除完整对象重复外，还检查忽略空白后的 `source_text` 重复、较长原文之间的包含关系及非空 `clause_number` 重复。任一问题都会令 `is_valid=false`，但程序不自动删除或改写模型原文，便于审计。

## 前缀复用与依赖

Step 1、Step 1B、Step 2 和 Step 3 与 Core 使用相同 system message、公共文本和 PDF 页面顺序，任务后缀始终放在全部图像之后，因此可以复用 vLLM 的多模态前缀缓存，同时不会把 Core 地图或字段事实传入 Clause。

```bash
python experiments/clause_extraction/run.py --pdf <PDF 路径> --max-pages <整份页数上限>
```

模型连接、采样参数、视觉页数默认值和 API Key 环境变量读取 `configs/settings.yaml` 与 `.env`。运行依赖 PyMuPDF、Pydantic、jsonschema、OpenAI Python SDK、httpx 和本地 vLLM OpenAI 兼容服务。完整产物见实验目录的 [`README.md`](../../experiments/clause_extraction/README.md)。
