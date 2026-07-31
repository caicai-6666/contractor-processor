# 项目初始化说明

## 已初始化内容

- `src/contract_processor/`：遵循架构文档的 domain、application、infrastructure、interfaces 与 bootstrap 分层。
- `configs/settings.yaml`：本地实际运行配置，包含 MLLM、Embedding、Reranker 三个独立的 vLLM 服务配置。
- `configs/settings.example.yaml` 与 `.env.example`：可复制的配置模板；Embedding 与 Reranker 模型名必须替换为本机已加载的模型名称。
- `description/fields/`：Core 与 Attribute 的字段定义源保持不变，由 `YamlFieldCatalog` 读取。
- `data/input/<batch_id>/`：运行时放置原始合同 PDF 的目录；敏感输入不提交版本库。
- `data/artifacts/<contract_id>/`：可选的调试页面图像目录；默认逐页在内存中渲染并直接发送给 MLLM，不写入此目录。
- `data/` 其余运行数据目录除 `.gitkeep` 外均被 Git 忽略。
- `tests/unit/`：领域频次去重规则的首个回归测试。

## 当前可用入口

```bash
python3 -m pip install -e ".[dev]"
contract-processor inspect-fields

# 未安装项目时，也可直接使用：
PYTHONPATH=src python3 -m contract_processor inspect-fields
```

该命令应显示当前 Core 与 Attribute 字段数量，可验证 YAML 字段库、领域模型和 CLI 的连接。

## 尚未实现的模块

- PDF 页面渲染与图像输入编码；
- OpenAI 兼容 vLLM 的真实合同页调用；
- LlamaIndex 字段向量索引与召回；
- LangGraph 的完整合同处理图；
- SQLite 审计存储与 Attribute 审核导出。

这些模块已有明确的目录和端口边界，后续实现不得将外部框架对象泄漏至 `domain` 或 `application` 的公共接口。
