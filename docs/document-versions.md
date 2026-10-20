# 第七阶段：文档版本与历史引用

## 行为与复用

沿用现有 DocumentRecord 作为一个具体版本的身份，不把原文档内容原地覆盖。上传新版本仍走 IngestionService、原格式校验、MinerU/本地解析、Embedding、Milvus、重试与删除补偿；每个版本使用独立 UUID 文件、解析缓存和 chunk ID，无需修改 Milvus schema 或重新入库历史文档。

新增两张小表：`document_series` 保存所有者、当前文档指针、下一个版本号；`document_versions` 将物理文档关联到版本族及版本号。版本号与族的组合唯一。计数器只递增，显式删除旧文件也不复用版本号。

首次普通上传为 v1。已有未纳管文档保持原 ID、文件、缓存和向量不动；版本列表将它视为虚拟 v1，GET 不写库。首次基于它上传新版或明确设为当前时，才在同一事务中补上版本关系。

## 使用流程

1. 在“资料”中点 `v1 · 版本`，查看版本号、入库状态、当前标记、创建时间和内容哈希。
2. 上传新版本，仍由现有入库任务处理。处理中、失败和就绪的候选均不会自动替换当前版；旧版继续服务。
3. 新版就绪后明确点击“设为当前”。切换只修改版本指针，不重写文件或向量。也可明确将旧版重新设为当前，实现回退。
4. “全部资料”默认只将每个版本族的当前 ready 版本传入检索；从聊天资料选择器明确指定某个旧版本 ID 时，仍可查询历史内容。

资料列表保留各具体版本，避免旧数据突然消失；`can_query` 表示可显式查询，不表示它一定进入“全部资料”。版本族没有当前版时不会自动挑一个历史文件替代。

![离线版本面板验收](screenshots/document-versions.png)

截图来自独立离线测试后端，展示 v2 设为当前后的反馈与 v1 保留，不是用户真实文档。

## 显式升级与配置

```powershell
# 备份后执行完整的平台版本迁移
.venv/Scripts/python.exe -m app.migrate

# 再在本机 .env 设置并重启后端
# DOCUMENT_VERSIONS_ENABLED=true
```

迁移负责按版本升级已有表并补齐依赖，不重建向量或改写原文件，也不在应用启动时自动执行。`bootstrap --document-versions` 保留为兼容入口，执行同一完整平台迁移链；它不再只创建两张缺失表。新部署的 `bootstrap --postgres` 也使用该迁移链。旧索引开启发布流程前还需按 [显式核验与绑定](index-adoption.md) 核查。

配置默认关闭，启用前需要升级，readiness 会检查对应结构。表已存在时，即便关闭管理入口，默认检索仍识别当前指针，避免把旧版本重新混入“全部资料”；关闭状态不新增版本标签或提供版本管理按钮。当前业务部署状态以 [功能补齐进度](replication-repair-progress.md) 为准。

## API

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/api/v2/documents/{id}/versions?offset=0&limit=20` | 当前用户的版本历史，分页返回 DocumentPage |
| POST | `/api/v2/documents/{id}/versions` | multipart `file`，以该版本所属族创建新版，202 后沿用现有任务调度 |
| POST | `/api/v2/documents/{id}/activate` | 将目标版本设为当前；JSON 必须显式提供 `expected_current_document_id`，允许 null |

示例切换请求：

```json
{"expected_current_document_id":"用户刚读取的当前版本 UUID"}
```

省略比较字段返回 422。其他用户的文档和版本族一律 404；未就绪、删除中或 Embedding 模型不匹配的版本不能激活；指针已改变返回 409 `DOCUMENT_VERSION_CONFLICT`，应刷新后让用户重新选择。

DocumentResponse 新增可选 `version_info`；KnowledgeCitation 新增可选 `document_version`，其内容只包括 `series_id/version_number/content_hash`。引用中的 `document_id/chunk_id` 仍绑定实际命中的物理版本。ChatService 在最终权限复核后从数据库补齐这份不可变来源快照并保存，不信任模型申报版本号；旧消息没有新字段也能照常显示。

## 一致性与删除边界

创建新版和首次纳管旧文档在一个数据库事务内完成。PostgreSQL 使用现有文档行锁和版本族行锁分配版本号，唯一约束作最后防线；同族不同基础版本的同时上传也不能占用同一版本号。激活时在版本族锁内比较当前指针，防止并发覆盖；锁忙沿用现有 RESOURCE_BUSY 处理。

原始内容哈希由上传流计算，重新处理前再次校验文件字节；发现源文件被外部篡改返回 `DOCUMENT_SOURCE_CHANGED`，不会把改变后的内容伪装成旧版本。失败新版不影响当前版。相同用户相同内容仍沿用现有去重规则，返回 409，不为相同字节创建重复版本；需要回退时使用旧版激活。

“不可变”指不以新版覆盖原内容，不等于禁止用户显式删除。原 DELETE 只删除选中的具体版本及其向量/文件，不连带清理其他版本；删除当前版会先清空指针，失败时保留 deleting 状态供补偿，不自动退回旧版。历史消息中的摘录、原始 UUID、行号、版本号和哈希不会被重写；源文件已被显式删除后不能再查询或恢复该源，仍可阅读已保存的引用摘录。所有版本都被删除后仅留下无内容的版本族计数记录，没有自动清库任务。

## 评估与验证

第六阶段在线评估同样保存版本来源快照，并继续要求显式文档范围。引用 schema 的新增可空字段影响规范化快照的哈希，因此合成样例审阅指纹与报告已同步重建，事实标签未改变。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_document_versions.py -q
.venv/Scripts/python.exe -m pytest -q
cd frontend
npm run build
npm run test:agents
```

离线用 SQLite、替身向量和脚本模型验证：新旧 ID/字节/向量独立、当前/显式历史范围、首次旧库纳管、重复上传回滚、失败与篡改拒绝、跨用户隔离、切换冲突、删除补偿边界、版本号不复用和历史消息版本快照。测试使用本地字符切分替身避开 tokenizer 下载，真实 tokenizer/Embedding 的表现不由这些用例证明。

真实 PostgreSQL 并发锁有单独的显式隔离测试：

```powershell
# 使用事先创建且数据库名包含 test 的独立测试库，沿用平台 TLS 要求
$env:RUN_VERSION_INTEGRATION='1'
$env:VERSION_TEST_DATABASE_URL='postgresql+psycopg://TEST_ROLE:TEST_PASSWORD@localhost/rag_versions_test?sslmode=require'
.venv/Scripts/python.exe -m pytest tests/integration/test_document_versions.py -q
```

该测试只针对显式测试 URL，不读取或清理真实平台库；初始化测试 schema，清理自己创建的随机记录。它验证同族并发分配的 NOWAIT 锁及过期指针冲突。当前没有运行真实 PostgreSQL/Milvus 验证，不能将 SQLite 成功当作并发锁已经线上验收。

后续仍需可靠 Worker、持久化 Run/Event 与 SSE 重放、共享权限和模型中心。本阶段不新增队列或多 Agent 框架。

2026-10-16 验证记录：完整后端回归 **249 passed、10 skipped、38 subtests passed**；补充删除失败边界后，版本与答案评估专项 **42 passed**。前端 vue-tsc / Vite 构建及 4 项 Node 测试通过；浏览器在独立离线后端验证版本历史、设为当前和旧版保留，截图见上。`uv lock --check`、bootstrap help、评估样例报告重建与 diff 空白检查通过。没有执行真实数据库升级、PostgreSQL 锁集成或付费模型调用；本阶段代码未自动提交或推送。
