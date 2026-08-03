# Elasticsearch 中文分词配置

> **适用范围：** 本文说明本地 Elasticsearch 的 `analysis-smartcn` 运行时依赖、安装验证与索引重建边界；不包含凭据或合同数据操作。

---

## 模块用途

说明本项目本地 Elasticsearch 的中文分词依赖、安装方式、验证方法和索引使用约束，保证中文合同文本的检索配置可复现。

---

## 主要职责

- 固定 Elasticsearch 与 `analysis-smartcn` 插件的版本匹配关系；
- 说明插件安装后必须重启服务的运维步骤；
- 约定中文文本字段在索引创建时使用 `smartcn` analyzer；
- 记录已有索引应用新 analyzer 时的重建与 reindex 要求。

---

## 当前配置

本地环境使用 Elasticsearch `9.4.4`，安装与 Elasticsearch 版本完全匹配的官方
`analysis-smartcn` 插件。插件提供 `smartcn` analyzer，并注册 `smartcn_word` 等底层
分词组件；业务 mapping 应优先使用完整的 `smartcn` analyzer。

插件安装目录为 `/usr/share/elasticsearch/plugins/analysis-smartcn`，服务由系统
`elasticsearch` 单元管理。密码仍只从项目根目录被 Git 忽略的 `.env` 读取，证书使用
`configs/settings.yaml` 中配置的公开 CA 证书路径，不在本文档或仓库中记录凭据。

> **版本边界：** 插件版本必须与正在运行的 Elasticsearch 主版本兼容；配置文件只保存连接方式与公开证书路径，绝不记录用户名、密码或令牌。

---

## 安装与启用

在目标机器执行（插件版本必须与 Elasticsearch 版本一致）：

```bash
sudo /usr/share/elasticsearch/bin/elasticsearch-plugin install --batch analysis-smartcn
sudo systemctl restart elasticsearch
sudo /usr/share/elasticsearch/bin/elasticsearch-plugin list
```

安装插件不会创建索引或修改合同数据；重启是让已运行的 Elasticsearch 加载插件的必要步骤。

> **运维边界：** 安装和重启会改变服务运行状态。执行前确认索引服务可维护，并按既有环境的变更流程操作。

---

## 验证方式

先确认服务可用，再调用 `_analyze` 检查分词结果。项目本地 HTTPS 配置可使用：

```bash
set -a; . ./.env; set +a
curl --noproxy '*' --cacert data/certs/http_ca.crt \
  -u "${ELASTICSEARCH_USERNAME}:${ELASTICSEARCH_PASSWORD}" \
  -H 'Content-Type: application/json' \
  https://127.0.0.1:9200/_analyze \
  -d '{"analyzer":"smartcn","text":"本合同由深圳柏莱科技有限公司提交合同签订地仲裁委员会裁决。"}'
```

成功响应应包含 `本`、`合同`、`深圳`、`科技`、`有限公司`、`仲裁`、`委员会` 等 token。
若出现 `failed to find global analyzer [smartcn]`，通常表示插件已安装但 Elasticsearch 尚未
实际重启，或插件版本与服务不匹配。

> **验证标准：** 只有插件列表与 `_analyze` 请求均成功，才可认为中文分析能力可用于建索引或查询。

---

## 索引使用方式

中文 `text` 字段必须在创建索引时指定 analyzer，例如：

```json
PUT contracts-v1
{
  "mappings": {
    "properties": {
      "content": {"type": "text", "analyzer": "smartcn"}
    }
  }
}
```

本项目的正式合同 mapping 由 `ElasticsearchMappingFactory` 根据字段定义生成；新增需要中文
检索的文本字段时，应在 mapping 生成逻辑中明确配置 `smartcn`，并补充 mapping 与检索回归测试。
不要直接修改已存在索引的 analyzer：analyzer 属于索引创建时的 mapping 配置，已有数据需要
创建新索引、写入新 mapping 后通过 reindex 迁移，最后再按发布流程切换索引别名。

正式入库索引及其验收索引的稳定中文文本投影同样显式配置 `smartcn`，并在用例初始化或首次
写入前校验现有 mapping。
对已经清空、无需保留数据的实验索引，可使用
`experiments/contract_ingestion_persistence/rebuild_empty_index.py` 进行带名称确认和零文档门禁
的重建；`clear_index.py` 仅删除文档并保留 mapping，不能使 analyzer 变更生效。

> **重建边界：** analyzer 属于 mapping 的创建期配置。变更后必须以新 mapping 重建索引，不能仅清空文档后继续使用旧索引。

---

## 依赖与注意事项

- 插件是本地 Elasticsearch 的运行时依赖，不应在应用启动时自动下载安装；部署脚本应显式安装并验证。
- 生产环境必须锁定与 Elasticsearch 完全匹配的插件版本，并在升级 Elasticsearch 时同步升级插件。
- 分词器只影响倒排索引的分析结果，不改变 `_source` 中保存的原始合同文本或结构化字段值。
- HTTPS、CA、用户名和密码的配置约定见
  [FastAPI 与 Elasticsearch 协议对齐](../reference/fastapi-elasticsearch-alignment.md)。
