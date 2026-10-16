# 第十三阶段：最终联调与验收

验证日期 2026-10-16，第十二阶段基线为 `889403e`，最终修复与本记录一起提交。所有业务数据、文档和账号均为独立环境中的合成测试数据；没有迁移或清理原平台库、文件及向量。

## 已取得的当前证据

| 范围 | 实际结果与边界 |
| --- | --- |
| 后端全量离线回归 | 最终 332 passed、15 skipped、38 subtests passed / 51.40s；跳过项为显式在线集成，另行执行 |
| 前端 | `npm run build` 成功（vue-tsc + Vite）；`npm run test:agents` 8 passed，覆盖 URL 提取、来源标签、SSE 游标、去重及重连逻辑 |
| 真实 PostgreSQL 18 + Milvus 3.0.0 | `tests/integration` 15 passed / 14.88s；版本行锁、Worker 领取/租约、Run 序号、共享 ACL 锁、模型配置与计量、三级树、会话隔离/级联、混合检索、Excel JSON 来源、父级读取、只读 SQL/超时/截断均经过真实服务 |
| 上述测试的隔离与清理 | 新建独立随机名 PostgreSQL 容器，tmpfs，无宿主卷；Milvus 独立随机名数据库，只含 validation_docs/validation_memories。完成后两者均删除，未清理既有集合或改动平台库 |
| 真实外部适配器 | [provider-smoke.json](provider-smoke.json)：Qwen 一条合成文本产生 1024 维向量；DeepSeek 回答 OK；Tavily 一次公开查询返回 5 条结果、报告 1 credit；Scrapling 直接读取 example.com 成功 |
| Ollama | 128 输出 token 探测失败，512 上限复测返回 OK；[完整 ChatService](ollama-chat.json) 后续在原 32768 上下文、2048 输出上限下返回“你好”，35.86s。没有以目录探测代替真实生成 |
| API 协作 | [api-acceptance.json](api-acceptance.json) 19 项检查通过：三 Agent 各委派一次、9 次模型调用，SQL=300、保修=12个月、页面标题=Example Domain；三级根块补充、主动断流后按游标续传、幂等重试、版本切换、共享新版本=24个月、撤权拒绝、跨用户隔离与真实计量 |
| 记忆与 Excel | [memory-excel-acceptance.json](memory-excel-acceptance.json) 6 项检查通过：记忆版本 2 的真实向量检索、跨用户拒绝、XLSX Worker 两行入库与最终 Specs 第 3 行的 77 W 引用 |
| 浏览器 | [browser-acceptance.json](browser-acceptance.json) 10 项观察通过：登录→文件选择器上传→Worker 完成→选择资料→真实流式问答→来源卡→刷新恢复→会话搜索；SQL/Excel 来源可查看。IAB 页面错误日志为空 |
| 管理员边界 | [operations-acceptance.json](operations-acceptance.json)：真实调用统计，普通用户改配置、读全局统计均为 403。目录状态如实保存，连接和凭据不出现在页面中 |

在线基础探测仅使用合成内容和公开查询，无真实用户文档或数据库结果。用量与失败均如实保留，不将模型目录可访问当作推理已通过。

## 需求核验清单

| 需求 | 离线/存储证据 | 真实联调证据 |
| --- | --- | --- |
| Supervisor/create_agent、三子 Agent、顺序委派、澄清/闲聊、固定模式授权 | tests/test_agent_v2.py、test_sql_agent.py | api-acceptance、route-acceptance、ollama-chat；SQL-only 仅委派 SQL，歧义直接 clarification |
| RAG 权限、Qwen、Milvus 混合检索、Excel 行级来源、父级/三级合并 | test_excel.py、test_parent_context.py、test_hierarchy.py 与真实存储集成 | 真实 embedding→独立 Worker→检索→最终引用；Excel 最终物理行和浏览器来源卡 |
| SQL 独立只读账号、表白名单、单条 PostgreSQL AST 校验、只读事务、默认 5 秒/最多 100 行 | test_sql_agent.py；真实 SQL 集成 4 项：聚合/截断、非法操作、超时、写权限拒绝 | 合成业务库与平台库分离，生成查询准确；来源只显示别名/查询，不显示连接串 |
| Tavily/Scrapling、本轮最多 3 URL、页面链接不自动获准、公网 DNS/端口/大小/重定向 | test_agent_v2.py、test_v2_foundation.py 的授权和固定 DNS/下载边界 | 组合任务直接读取指定网页且未搜索；web-search-clean 为正常搜索；route-acceptance 保留一次未授权链接拒绝和子 Agent 格式故障 |
| 统一证据/引用，来源不可信，主 Agent 最终流，失败缺口，6 次总委派/每 Agent 2 次，共享 deadline/模型预算 | test_agent_v2.py 的伪造引用、流式隔离、修复/拒绝、预算/时限、降级和请求间隔离；test_knowledge_quality.py | 三类事实与合法来源逐项核对；真实 SSE 续传无重复序号，最终助手消息唯一；故障在最终答案中披露 |
| 保留 /api/v2 会话、登录、记忆、模型、SSE、来源保存和历史；前端过程/SQL 卡 | test_auth_memory.py、test_chat_runs.py 与 8 项前端逻辑测试 | 浏览器完整链路、真实记忆检索、真实 Ollama、持久化事件和 SQL/Excel 卡截图 |
| 后续阶段：精排、规划/证据判断、答案评估、版本、Worker、重放、共享、模型中心、三级分块 | test_rerank.py、test_knowledge_quality.py、test_answer_evaluation.py、test_retrieval_evaluation.py 及对应阶段测试 | 真实存储集成与同一隔离环境中的版本/Worker/共享/模型中心/三级检索协作；付费精排/云解析在线边界见下 |
| 运行与学习材料、保留数据、每阶段推送 | README、各阶段配置/调用链说明与 git log | 下列演示步骤、JSON 记录及截图；迁移保持显式，生产 .env 未更改 |

## 本阶段修复

- 同标识重试原先先锁会话行，可能在问答准备/保存阶段误报 RESOURCE_BUSY。现在先匹配、校验原 Run，新请求仍锁会话；不重复模型/工具执行，不放宽载荷冲突或新请求并发。原始记录见 [combined-initial.json](combined-initial.json)。修复后真实 PostgreSQL 持锁回归 1 passed / 0.90s，离线 Run 专项 12 passed，真实 API 即时同键重试通过。
- SQL 默认 100 行，但配置曾允许 1000；现配置上限也固定为 100，验证 101 被拒绝，符合原计划硬边界。

## 演示与复核

隔离业务表 `sales.orders(id, product, amount)` 的三行分别为 `(1, NX-DEMO, 100)`、`(2, NX-DEMO, 200)`、`(3, OTHER, 900)`，仅向独立账号授予该表 SELECT。上传 Markdown“NX-DEMO 的保修期为12个月”，选择该文档，用 auto 提问：

> 请完成三件事并逐项引用来源：1）从业务库 sales.orders 查询产品 NX-DEMO 的 amount 合计；2）从所选私有文档查询 NX-DEMO 的保修期；3）直接读取 https://example.com 给出页面标题。不要联网搜索。

预期为 `300`、`12个月`、`Example Domain`，各有可打开来源。上传改为 `24个月` 的新版本并显式激活，再通过共享库查询，应引用新文档。Excel 演示使用 `Specs` 表头“产品/功率W/维护周期(月)”，第 2 行 OTHER/900/12、第 3 行 UI-EXCEL/77/6，询问 UI-EXCEL 功率，应引用第 3 行。

启动和配置见 [多 Agent](../multi-agent-guide.md)、[版本](../document-versions.md)、[Worker](../durable-ingestion.md)、[重放](../durable-chat-runs.md)、[共享](../organization-sharing.md)、[模型中心](../model-center.md)、[三级检索](../hierarchical-retrieval.md)。验收在独立 test 库显式初始化并开启相关开关，API/Worker 共享临时上传和解析目录，没有对原平台执行 bootstrap。

```powershell
.venv/Scripts/python.exe -m pytest -q
cd frontend
npm run build
npm run test:agents
```

真实集成按 tests/integration 各模块的 RUN_*、TEST_DATABASE_URL 显式运行，仅连接独立 test 库；存储测试会初始化表/集合，不要指向原平台。在线验证产生实际模型/搜索用量，离线测试默认不调用付费服务。

截图：[组合问答与 SQL](../screenshots/acceptance-combined-sql.jpg)、[浏览器上传后引用](../screenshots/acceptance-browser-rag.jpg)、[Excel 第 3 行](../screenshots/acceptance-excel-row.jpg)。

## 保留的失败与边界

- [route-acceptance.json](route-acceptance.json) 中复杂联网任务曾因尝试未注册链接被拒绝，随后子 Agent 返回格式错误。合法搜索/已读页面来源仍可用，最终回答披露缺口；它标为降级，不冒充干净成功。正常搜索另存独立记录。
- DeepSeek 生成成功，但目录响应未列出配置中的精确名称 deepseek-chat，目录探测保留 MODEL_NOT_LISTED。目录结果不等于推理结果；Ollama 目录与真实生成通过。
- 早期 Ollama 128 token 探测失败，512 复测成功；完整 ChatService 使用原 32768/2048 配置通过。失败记录未删除。
- 本轮未启用真实 MinerU 云解析和付费精排，不宣称在线质量；接口、缓存、超时、非法响应及降级有离线契约测试。答案评估的合成控制样例不是线上质量评分。
- Edge 扩展限制自动上传，未改扩展权限；同一合成文件已由 IAB 真实文件选择器上传成功。原浏览器标签未改动。
- 本次是功能与边界验收，不是生产容量、长时压测或任意模型始终正确返回结构化输出的承诺。

## 清理核验

验收结束时已确认 active Run、待处理入库任务和未完成模型调用均为 0。两套完整联调环境的 PostgreSQL 容器与专用 Milvus 数据库均已删除；API 8780、前端 5179 无监听，验收浏览器标签已关闭，临时凭据状态文件已删除。清理范围只包含本次创建的随机名资源，原有 Milvus/Dify 容器和用户浏览器标签保留。
