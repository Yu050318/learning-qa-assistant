# 第十阶段：组织、角色与共享知识库

本阶段实现组织范围的知识库权限，复用原账号、私有文档、向量索引、Supervisor、引用和消息存储。共享只增加授权关系，不复制文档、缓存或向量。默认关闭，不更改已有资料。

## 权限模型

| 身份 | 组织成员管理 | 创建/删除共享库、授权 | 读取共享库 | 发布文档、取消发布 |
| --- | --- | --- | --- | --- |
| 组织所有者 | 普通成员、管理员；本人不可被移除或降级 | 全部 | 全部 | 全部 |
| 组织管理员 | 普通成员 | 全部 | 全部 | 全部 |
| 知识库 editor | 无 | 无 | 被授权的库 | 被授权的库 |
| 知识库 viewer | 无 | 无 | 被授权的库 | 无 |
| 未授权成员、其他组织用户 | 无 | 无 | 无 | 无 |

发布时所有身份都只能选择**自己已就绪的文档**，管理员也不能凭 UUID 发布别人的私有文件。editor 可以取消该库已有文档的发布；取消发布和删除库/组织只删除关系，保留原文件、向量、私人聊天。原始资料的版本切换、重试、读取原管理记录和物理删除仍要求文档所有者，授权读取不包含这些管理能力。

成员必须先有已注册的账号，管理员按用户名添加，不发送邮件或邀请。所有者任免管理员，所有者身份在此阶段固定，不提供转让。移除成员通过外键级联撤销其全部知识库授权，重新加入不会恢复旧授权；已经发布的资料保留，管理员可取消发布，原所有者仍可删除自己的源。每个组织独立授权，不能把另一个组织的成员写入本库 ACL。

这里的租户边界覆盖组织知识库。账号、私有会话和记忆仍是原用户级隔离，SQL Agent 仍使用首期单个服务端只读业务数据源及白名单，本阶段没有将 SQL 业务行或模型配置改成租户独立资源，不应将知识库隔离解读成业务数据库的租户行隔离。

## 使用方式

```powershell
# 只添加组织、成员、知识库、授权、发布关系五张表
.venv/Scripts/python.exe -m app.bootstrap --sharing

# 服务端 .env 设置后重启 API：
# SHARING_ENABLED=true
```

前端“资料 → 组织与共享库”支持创建组织、添加/移除成员、管理角色、创建/删除库、授予/撤销 viewer/editor、发布/取消发布已有个人资料。普通成员只看到自己的组织与已获授权的库。知识库管理表单沿用现有色彩、字体和控件，并提供字段标签、忙碌状态和错误信息。

聊天“选择资料与共享库”可勾选共享库，每次最多 20 个；只有显式选择后才加入本轮，默认仍是自己的私有资料。共享库和私有资料可组合检索，SQL/Web 模式拒绝知识库参数。指定 `document_ids` 时限制在个人资料与本轮获准共享库的并集，不会因为知道 UUID 就获得访问权。取消所有文档选择表示默认范围，与原行为一致。

新版本使用独立物理文档 ID，因此必须单独发布。默认检索只选已发布且为当前版本的文档；如果新当前版尚未发布，不会回退到旧版。显式 `document_ids` 可以查询仍被发布的历史版本；共享库界面只提供整库选择，按历史 UUID 精确查询使用 API。关闭版本管理开关也不会让已纳管的历史版本混入默认检索。

## 实现链路与复用

新增 `SharingService` 和 `app/api/sharing.py` 管理五张附加表；原用户、文档和 Milvus schema 均保持原结构。知识库可发布多位成员的文档，解析与入库仍走原流程，源归属不变。

`ChatService` 把本轮获准文档解析成 `{document_id: owner_id}`，冻结到 `RunContext`。Supervisor 每次检索前重新校验组织成员和 ACL，再调用原 Retriever。Retriever 只生成一次 Query Embedding，按文档真实所有者分组调用 Milvus，合并候选后全局去重、Rerank 一次、选 Top K，父上下文按同一所有者扩展。每个返回片段必须匹配该组的真实 owner 和文档 ID，伪造另一个用户的片段直接拒绝。分组调用共享请求 deadline。

最终答案保存前再次校验全部冻结范围。PostgreSQL 下最终事务对组织成员、知识库和 ACL 持有读锁，授权写入、取消发布和库删除使用冲突的写锁，防止撤销在最终权限检查和消息提交之间穿过。版本引用按源文档所有者补充版本号和哈希，最终答案、SSE done 和历史消息继续复用第七/九阶段逻辑。权限变化或文档不可用时不提交最终助手消息。

已经在获准时读取或发送的草稿无法从客户端撤回，撤销不是远端执行的硬取消。历史聊天和其中的引用摘录属于提问用户已保存的会话，撤销共享后仍可读；撤销禁止新的检索和源管理访问，不重写历史记录。共享不共享聊天、记忆、模型密钥或 SQL 凭据。

## 接口

- `GET/POST /api/v2/organizations`：列出已加入组织、创建组织。
- `DELETE /api/v2/organizations/{id}`：所有者删除组织。
- `GET/PUT /api/v2/organizations/{id}/members`：管理员查看、添加或更新角色。
- `DELETE /api/v2/organizations/{id}/members/{user_id}`：移除成员。
- `GET /api/v2/knowledge-bases`：当前用户获准知识库。
- `POST /api/v2/organizations/{id}/knowledge-bases`：管理员建库。
- `DELETE /api/v2/knowledge-bases/{id}`：管理员删除库。
- `GET /api/v2/knowledge-bases/{id}/grants`：管理员查看显式授权。
- `PUT/DELETE /api/v2/knowledge-bases/{id}/grants/{user_id}`：授予或撤销 viewer/editor。
- `GET /api/v2/knowledge-bases/{id}/documents`：列出已发布文档的必要状态。
- `PUT/DELETE /api/v2/knowledge-bases/{id}/documents/{document_id}`：发布自己的就绪文档、取消发布。

问答、流式、Run 和上下文估算请求新增可选 `knowledge_base_ids`，UUID 数组，默认空。所有接口沿用 Bearer 认证；未授权的组织/库/文档使用 404，已知范围内缺少管理角色为 403，非法角色/模式由 schema 拒绝。身份和组织 ID 不由模型决定。`capabilities.features.sharing` 控制前端入口。

## 验证与回退

```powershell
.venv/Scripts/python.exe -m pytest tests/test_sharing.py -q
.venv/Scripts/python.exe -m pytest -q
cd frontend
npm run build
npm run test:agents
```

离线测试覆盖成员和 ACL 双重要求、所有者保护、管理员越权拒绝、跨组织隔离、发布仅限自己的 ready 文件、撤销/删除级联、重新加入不恢复权限、版本默认范围、混合所有者检索、单次 Embedding/Rerank、伪造 owner 拒绝、真实 Supervisor 共享 RAG 与版本引用，以及回答期间撤销阻止最终消息。SQLite 测试不能证明 PostgreSQL 锁行为。

```powershell
# 专用隔离库，数据库名必须含 test；不使用实际平台库
$env:RUN_SHARING_INTEGRATION='1'
$env:SHARING_TEST_DATABASE_URL='postgresql+psycopg://TEST_ROLE:TEST_PASSWORD@localhost/rag_sharing_test?sslmode=require'
.venv/Scripts/python.exe -m pytest tests/integration/test_sharing.py -q
```

该用例验证最终事务持有 ACL 快照时，另一个连接撤销会因锁超时被拒绝；最终事务结束后撤销生效。已在临时 PostgreSQL 18 容器通过，与版本、Worker、Run/Event 和只读 SQL 共 8 项真实 PostgreSQL 测试一同完成，见[验证记录](validation/postgresql-stage10.md)。真实向量服务和浏览器多账号验收仍待最终联合验收；没有升级现有数据库或调用收费模型。关闭 `SHARING_ENABLED` 并重启 API 可隐藏共享入口并拒绝共享查询，保留已有五张表、源资料和历史回答。

下一阶段为模型中心，随后补齐完整三级分块及真实环境联合验收。

2026-10-16 验证记录：全量后端 **297 passed、13 skipped、38 subtests passed**；随后补充旧 Run 请求指纹兼容及共享 Run 组合后，问答/共享专项 **25 passed**。前端 vue-tsc/Vite 构建和 **8 项 Node 测试**通过，`uv lock --check`、diff 空白检查通过；26 个变更源码/文档文件未命中当前配置的长密钥值。第九阶段 `9fc2f1f` 已推送 GitHub main。后续预验收已通过独立 PostgreSQL 的 8 项集成测试；真实平台库升级、真实向量联调和浏览器多账号验收未执行。
