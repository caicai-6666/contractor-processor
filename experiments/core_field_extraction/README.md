# 两步 Core 字段提取实验

本实验验证本地 vLLM 多模态模型对单份合同 PDF 的 Core 字段提取能力，实际模型由 `configs/settings.yaml` 配置，不依赖 `src/` 中的正式工作流。

## 流程

1. 将 PDF 页面在内存中渲染为 PNG，调用 MLLM 生成“合同理解地图”。Step 1 必须逐条穷举可见页面中的金额、价格、税率、税费、运费、保险费、服务费、付款和其他费用原文，不提前合并或计算。
2. 程序保留第一步 JSON 原始结构，同时将其确定性转换为缩进 bullet 文本作为 Step 2 公共上下文。随后按 [`core.yaml`](../../description/fields/core/core.yaml) 顺序逐字段调用模型。非 object 字段及 object 的每个直属子字段均使用 `raw_value/reason/status/value`；不生成根级 reason，程序汇总对象状态并按 field_id 合并。

第一步由 Pydantic 生成 JSON Schema；第二步从 `core.yaml` 的递归 output 定义动态生成单字段 Schema，并通过 OpenAI Python SDK 的 `response_format.type=json_schema` 交给 vLLM。回复由 `jsonschema` 与 Pydantic 再次校验。`fields` 只允许当前唯一 field_id。Core 与 Clause 分别生成自己的 Step 1 结果，但读取完全相同的公共前缀，并把各自任务放在相同 PDF 图像之后，以复用前缀而不混合语义上下文。

## 安装与运行

先启动本地 vLLM，并确认 [`configs/settings.yaml`](../../configs/settings.yaml) 的 `models.mllm` 配置正确。然后在已激活的 Conda 环境中执行：

```bash
python -m pip install -e ".[experiments]"
python experiments/core_field_extraction/run.py --pdf "data/input/柏莱-深圳现象光伏科技有限公司0908_已签章.pdf"
```

在 IDE 中直接运行 `run.py` 时，修改文件底部的 `DEFAULT_PDF_PATH`，然后点击运行即可；该变量会显式传入 `main()`。命令行的 `--pdf` 是可选覆盖项，可传入绝对路径或相对于项目根目录的路径。如需看到终端中的完整渲染提示词：

```bash
python experiments/core_field_extraction/run.py \
  --pdf "data/input/柏莱-深圳现象光伏科技有限公司0908_已签章.pdf" \
  --print-prompts
```

## 结果与上下文指标

每次运行会创建 `experiments/outputs/core_field_extraction/<UTC 时间戳>/`，该目录已被 Git 忽略。其中包括：

- `00_common_prefix_prompt.txt`：与 Clause 实验共用、位于 PDF 图像之前的稳定文本前缀。
- `01_understand_contract_prompt.txt`、`02_extract_core_common_prompt.txt`：位于 PDF 图像之后的 Core 任务提示词；第二步注入 Step 1 的 bullet 版本。
- `01_contract_understanding_bullets.txt`：由校验后的 Step 1 JSON 确定性转换的缩进条目文本，是 Step 2 实际读取的合同理解上下文。
- `01_contract_understanding.json`、`02_core_extraction.json`：经 Pydantic 校验后的分步结果；`final_core_extraction.json` 与 Step 2 的确定性合并结果一致。
- `02_fields/<序号_字段>/`：每个字段实际发送的 `schema.json`、图像后的 `prompt_suffix.txt`、`raw_response.json/.yaml`、`metrics.json` 和 `extraction.json`；失败字段另有 `failure.json`。
- `02_field_manifest.json`：第二步字段总数、成功/失败状态、对象汇总状态、未提取字段、聚合 usage、标量字段 reason、对象子字段 reasons、指标和 document_id 一致性。
- `01_raw_response.json`、各字段目录的 `raw_response.json`：服务原始响应；
- 对应的 `.yaml` 文件：原始响应的便于人工阅读版本。模型内容为完整 JSON 时会在 YAML 中展开为嵌套结构；截断内容则以 YAML 多行块保留原始字符串；
- `metrics.json`：每一步的 `prompt_tokens`、`completion_tokens`、`total_tokens`、耗时、图像数量和字节数；
- `field_coverage_validation.json`：是否遗漏或增加 Core 字段，以及包络是否违反状态、规范值和原始值的业务约束。
- `analysis.md`：人工分析该次实验时持续追加的统一日志；每条记录包含带时区时间、证据链接、原因判断、建议和验证状态。实验运行器本身不会创建该文件。

`prompt_tokens` 是否包含视觉 token 取决于 vLLM 返回的 usage 口径；本实验原样保留服务返回值，并同时记录图片数量和字节数，便于和服务日志交叉核验。

## 注意事项

- `models.mllm.context_window_tokens` 必须与 vLLM 的 `--max-model-len` 保持一致，当前 Instruct 服务按最多五页合同设置为 65536；`--max-model-len` 参数仅用于临时覆盖和记录。
- 为容纳多页金额与费用原文清单和复杂单字段对象，第一步与第二步每字段的代码上限均为 6144 token，且仍受 `settings.yaml` 中更小的生成上限约束。
- 所有 Core 任务位于共享文本和合同图像之后；既可与 Clause 复用公共多模态前缀，也可在逐字段请求间继续复用更长的 Core Step 2 任务前缀。各字段 Schema 不参与模型 KV 前缀。
- vLLM 的结构化输出服务建议使用 xgrammar 并关闭任意 JSON 空白，即启动参数包含 `--structured-outputs-config '{"backend":"xgrammar","disable_any_whitespace":true}'`，避免合法空格、换行或制表符形成低熵循环。
- 第二步字段请求被截断或未通过结构/业务校验时，运行器保存失败产物并继续后续字段，不再执行多字段拆分或同形式重试；最终 `field_coverage_validation.json` 会列出相应缺失字段。连接错误和程序错误不会被吞掉。
- 即使模型输出被截断而未形成合法 JSON，运行器也会先保存该步的原始响应与 usage，便于检查 `finish_reason` 和上下文占用。
- 非 object 字段和 object 的每个直属子字段均按 `raw_value/reason/status/value` 生成，数组作为一个直属子字段处理。顶层及对象外层均不生成 reason。
- object 外层 status 不由模型生成。程序按子字段状态汇总：任一 found 即为 found，否则依次采用 conflicting、ambiguous、全 not_applicable，其他组合为 not_found。
- object 子字段的 `out_of_scope` 用于保留存在但不属于采用口径的原文，此时 raw_value 非空、value 为 null。ambiguous/conflicting 同样保留候选原文；not_found/not_applicable 才要求 raw_value/value 同时为 null。
- 金额与费用相关字段必须逐条参照 Step 1 的金额清单并回看合同图像。计算输入必须同时存在于清单和图像中；运费、保险费、服务费等组成必须参与口径比较，不能只凭总额和税率计算。
- `contract_amount` 不再输出独立未税金额子字段。未税价格、未税总额和不含税总价仍由 Step 1 穷举，并保留在 `source_amount_text` 中；不得因此把已明确的金额子字段标为 ambiguous。
- `contract_validity_period.start_date` 不得直接复用签订日期或生效日期；只有合同明确将该日期定义为整体有效期或合作周期起点时才可采用。
- 当前实验故意不执行模型复核；Step 2 的缺失字段和非法包络会原样记录在 `02_field_manifest.json` 与 `field_coverage_validation.json`，便于单独衡量前两步能力。
- 付款、交付/服务、争议解决等条款型内容已移出 Core，计划由独立 Clause 层完整提取；本实验不再输出这些字段。
- 若本地 vLLM 未启用 API 鉴权，可保持 `.env` 中的 MLLM 密钥为空；脚本会传入占位值 `EMPTY`。
- 为确保连接 `127.0.0.1` 的 vLLM，实验客户端会忽略终端中的 HTTP/SOCKS 代理环境变量。
- 运行器会先访问 `<base_url>/models` 验证 vLLM 服务；若端口未监听，会在发起模型请求前给出明确提示。
- 此目录只用于验证。经验证可复用的实现才应迁移到 `src/` 并补充正式测试。
