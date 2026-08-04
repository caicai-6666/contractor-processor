# 项目初始化说明

> **适用范围：** 本文说明本地初始化产物、正式可用入口与当前实现边界。项目架构和业务规则请阅读相应专题文档。

---

## 已初始化内容

- `src/contract_processor/`：遵循架构文档的 domain、application、infrastructure、interfaces 与 bootstrap 分层。
- `configs/settings.yaml`：本地实际运行配置，包含 MLLM、Embedding、Reranker 与 Elasticsearch 配置。
- `configs/settings.example.yaml` 与 `.env.example`：可复制的配置模板；Embedding 与 Reranker 模型名必须替换为本机已加载的模型名称。
- `data/definitions/`：生产 Core、Attribute、Clause、摘要机器规范，以及规划中的独立
  Discovery Core/Attribute 初始目录，均由显式配置路径加载。
- `description/fields/` 与 `description/contract-summary/`：只保存业务设计和人工说明文档。
- `data/input/<batch_id>/`：运行时放置原始合同 PDF 的目录；敏感输入不提交版本库。
- PDF 页面在工作线程中渲染并直接发送给 MLLM，正式流程不保存调试页面或运行报告；实验
  输出统一位于 Git 忽略的 `experiments/outputs/`。
- `tests/unit/`：领域频次去重规则的首个回归测试。

> **数据边界：** 原始合同和实验输出均不进入版本库；正式处理不保存页面、Prompt、原始响应或运行报告。

---

## 当前可用入口

```bash
python3 -m pip install -e ".[dev]"
python -m contract_processor.interfaces.cli.inspect_fields
```

该命令应显示当前 Core 与 Attribute 字段数量，可验证 YAML 字段库、领域模型和 CLI 的连接。

> **配置原则：** 实际运行配置与模板配置分离。模型名称、凭据和环境相关参数必须由 `settings.yaml` / `.env` 提供，不能散落在业务代码中。

---

## 已实现的正式工作流模块

- PDF 单次异步隔离渲染与三条 MLLM 线路共享；
- `production`：`prepare` 后并发执行 Core→（非空时 Attribute）、Clause、Abstract，并在
  汇合后输出候选的异步 LangGraph 图；
- `discovery`：Core/空 Core → 固定 Attribute → Field Discovery 的独立图，不注册 Clause 和 Abstract；
- 生产 0 Core 启动门禁、发现 0 Core 空策略和字段目录空状态校验；
- 正式 `FieldDiscoveryService`：候选级准入、Embedding Top 5、关系图、并发组级收敛和全局门禁；
- `DiscoverFieldsFromBatch` 两阶段父图：冻结最终字段后按“单合同、单字段”回扫并聚合频率；
- `interfaces/cli/` 下的无落盘单文件和批量入口；
- FastAPI 前后端 DTO 与依赖边界（当前无路由）；
- 由 Core YAML 异步生成的 Elasticsearch mapping 与专家确认异步写入 Repository。

尚未实现专家 UI、候选人工晋级写回、Attribute Profile 版本治理和 FastAPI 路由。当前
Attribute 规范源已包含 10 个 `0.3/draft` 字段，并由 production 固定提取器逐字段
处理；禁止跳过非空目录或静默退化成空数组、关键字匹配数组。正式持久化必须位于专家最终
校验之后。目标模式见
[Attribute 双运行模式设计](../architecture/attribute-operating-modes.md)，当前生产边界见
[合同信息化处理工作流](../architecture/contract-information-workflow.md)。
已确认的字段发现目录隔离、新候选专用内存向量池、Top 5 三分类和全量回扫统计见
[字段发现两阶段工作流](../architecture/field-discovery-workflow.md)。

> **当前限制：** 发现结果仍是待专家审核的知识包，不能自动写入生产字段目录或 Elasticsearch；未实现的专家治理与 FastAPI 路由必须保持未注册或 fail closed。
