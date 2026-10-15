# Supervisor 多 Agent 实现与学习指南

## 1. 从已有 Demo 复用什么

本次是在当前 Demo 上改造，没有复制 SuperMew 的 Run/Event、Worker、模型中心等平台。

| 能力 | 复用位置 | 本次变化 |
| --- | --- | --- |
| 登录、用户作用域、会话与落库 | `app/application/chat.py`、既有仓储 | 沿用 ChatService，传递新模式和本轮 URL |
| 模型选择、有限降级、预算与用量 | `app/workflows/agent.py` | 保留 ModelRuntime、AnswerStreamParser，移除旧单 Agent 工作流 |
| RAG | `app/application/retriever.py`、原 Embedding/Milvus | 提取为 RAG Agent 的工具；保留用户与文档范围二次校验 |
| 联网 | 原 TavilySearch、ScraplingWebReader | Web Agent 复用适配器；增加本轮 URL 授权和逐跳重定向校验 |
| 证据与引用 | `app/application/evidence.py` | 增加 SQL 来源，正文读取可以补全同 URL 的搜索摘要 |
| 界面与流式传输 | 原聊天组件、store、POST SSE | 增加 Agent 状态、SQL 模式和 SQL 来源卡 |

## 2. 阅读与请求调用链

建议依次阅读 `app/workflows/agent.py` 的 ModelRuntime、`app/workflows/supervisor.py`、SQL 适配器，最后看 API 与前端。

```text
ChatView 提交 question / search_mode / web_urls
  → /api/v2/sessions/{id}/messages[/stream]
  → ChatService：身份校验、文档范围、历史、用户消息落库
  → SupervisorWorkflow.run：创建请求独享的 Runtime、Registry、计数器与锁
     → Supervisor(create_agent)
        → delegate_rag → RAG Agent → search_knowledge → 原 RetrieverService
        → delegate_sql → SQL Agent → describe_sql_schema → execute_sql
        → delegate_web → Web Agent → search_web / read_web_page
     → Supervisor 最终 JSON → 引用校验（最多一次修复）
  → ChatService：最终答案、引用及运行元数据落库
  → done / 历史 API → 相同消息与引用界面
```

Supervisor 的工具是委派工具，每个子 Agent 自己持有受限工具并运行工具循环；不是让同一个 Agent 换三个名称。
SQL＋Web 任务可在一轮组合执行。LangChain 可能同时发出多个工具调用，所以委派入口用请求内锁串行化；子工具也串行修改共享证据与计数器。
请求之间不共享证据、预算、状态或队列。只共享既有服务适配器。

子 Agent 返回 `status / summary / sources / failures`。来源编号由本轮 Registry 分配，不能由模型创建。
子 Agent 原始回复不进入答案增量；仅 Supervisor 的 `answer` 字段流式输出。增量属于临时内容，`done` 才是已校验且落库的权威答案。

## 3. 模式与网页输入

- `knowledge` 仅提供 RAG 委派工具；`sql` 仅提供 SQL；`web` 仅提供 Web；`auto` 允许组合。
- `web_urls` 是可选列表，最多 3 个 HTTP(S) 公网地址，`knowledge/sql` 拒绝该字段及 `web_query`。
- 前端从当前输入中识别 URL；API 调用者也可显式提交。历史消息、网页正文及子 Agent 输出不会自动扩大 URL 权限。
- 指定网页可在没有 Tavily 配置时直接用 Scrapling 读取。需要同时搜索时，提交可公开的 `web_query`；只指定网页且没有公开搜索词时不会自动把整段问题发送到 Tavily。
- Tavily 继续使用冻结公开词，SQL 结果、知识库内容和历史不会拼入搜索请求。此约束限制了根据私有结果自动生成新公开搜索词的能力。
- 搜索得到的 URL 可继续读取；固定采集工具保留，最多读取配置列表前三页，不递归、不自动写入 Milvus。
- 每次重定向前检查协议、端口和公网 DNS；curl 传输固定到已校验 IP、关闭环境代理，避免解析结果变更绕过校验。正文继续由 Scrapling 提取。下载过程中限制 2 MiB，仅接受 HTML，并限制正文字符与请求 deadline。

请求示例：

```json
{
  "question": "查询各产品销量，并结合 https://example.com/news 的公告分析",
  "search_mode": "auto",
  "web_urls": ["https://example.com/news"],
  "model_provider": "deepseek"
}
```

## 4. 业务只读数据库配置

服务端 `.env`：

```dotenv
SQL_DATABASE_URL=postgresql+psycopg://agent_reader:替换密码@localhost:5432/agent_business?sslmode=require
SQL_ALLOWED_TABLES=["sales.orders","sales.products"]
SQL_SOURCE_NAME=产品业务库
SQL_TIMEOUT=5
SQL_MAX_ROWS=100
```

业务库必须与平台数据库分开；该账号只授予所需表 SELECT、schema USAGE 和数据库 CONNECT。
凭据不进入模型、公开响应或来源卡。连接配置留空时 SQL 执行返回 `SQL_UNAVAILABLE`。

下面由数据库管理员在**独立业务测试库**执行，替换账号密码；不要直接用于现有生产库。角色不能拥有业务表/schema、写入、CREATE 或管理权限，也不能继承这些权限。

```sql
CREATE ROLE agent_reader LOGIN PASSWORD '替换为随机密码';
GRANT CONNECT ON DATABASE agent_business TO agent_reader;
GRANT USAGE ON SCHEMA sales TO agent_reader;
GRANT SELECT ON sales.orders, sales.products TO agent_reader;
```

若数据库通过 PUBLIC 授予 CREATE，需要管理员先评估影响再撤销相应授权；应用不会自动修改角色权限。
首版数据源对所有登录用户共享，因此白名单只能包含他们均有权阅读的数据。平台用户隔离仍由既有路径负责；尚无业务数据按用户授权层。

执行器采用 sqlglot AST、显式 `schema.table` 白名单、函数/类型白名单、只读事务、只读角色检查、statement/lock timeout 和外层结果 LIMIT。
CTE、聚合、连接、UNION 可以使用；写入、多语句、锁、SELECT INTO、系统表、任意函数/自定义类型不能执行。
仅支持普通表，拒绝视图、外部表和分区父表；使用 ONLY，防止继承子表越过白名单。函数白名单有意有限，无法表达的查询应反馈限制，不通过放开任意函数解决。
必须先读取获准 schema 再执行查询。返回最多 100 行，单元格和总结果内容也有限额；截断会进入证据和来源卡。

## 5. 预算、错误和事件

默认共享预算：模型 16 次、工具 12 次、委派 6 次、每个 Agent 2 次，包含模型降级和答案修复。原 `AGENT_TIMEOUT` 控制请求 deadline。
模型降级只重试当前模型步骤，不重新执行已完成的查询/抓取。旧的按 RRF 阈值强制联网策略不再参与调度；现在由 Supervisor 根据意图选择 Web，RAG 内容不足可以再次委派。

工具故障返回稳定错误码，子任务返回 failed/partial，最终 metadata 的 failures 保留具体缺口；全局超时/预算耗尽直接结束请求。
网页、SQL 和文档内容均不可信，不展示模型内部推理。

新增 SSE 事件 `agent_started / agent_finished / agent_failed` 携带 `agent / status / error_code`，继续使用既有递增 sequence。
最终 `run_metadata.agent_events` 和 SQL citations 保存到原 JSON 字段，不需要改表或清库。
本次仍是单进程开发形态，未实现持久化 Run、SSE 断线重放或多 worker。

## 6. 验证与演示

```powershell
.venv/Scripts/python.exe -m pytest -q
cd frontend
npm run test:agents
npm run build
```

`test:agents` 使用 Node 自带测试运行器和 TypeScript 类型剥离，需要支持该功能的 Node（建议 22.18+）。不新增前端测试框架。
测试覆盖真实 LangChain Agent 调度（脚本模型）、SQL＋Web 汇总、RAG 作用域、指定网页、不泄露子回复、引用修复、模型降级和共享预算。
SQL AST 校验与执行器使用离线测试；不会在普通 pytest 中访问真实业务数据。

真实 PostgreSQL 集成测试必须显式配置**可创建临时库/角色的隔离测试服务器**，不能填生产管理员连接：

```powershell
$env:RUN_SQL_INTEGRATION = '1'
$env:SQL_TEST_ADMIN_URL = 'postgresql://测试管理员:密码@localhost:5432/postgres'
.venv/Scripts/python.exe -m pytest tests/integration/test_sql_readonly.py -q
```

该测试创建随机命名数据库与只读角色，结束时删除，验证聚合、结果截断、百分号查询、超时与权限拒绝。
初次实现环境 Docker 引擎未启动、WSL 未发现 PostgreSQL 服务端工具，真实 SQL、外部模型、Tavily/Scrapling 在线效果未验证；离线替身结果不能当作线上成功。

2026-10-15 验证记录：后端全量 80 passed、7 skipped（隔离服务未配置）；前端 3 项 Node 测试、vue-tsc 和 Vite 构建通过；`uv lock --check` 与 diff 空白检查通过。后续补充的日期聚合/类型拒绝用例也通过针对性测试。
浏览器使用离线业务/网页替身和脚本模型，经过真实 Supervisor → 子 Agent → SSE → Vue 链路，验证 SQL＋Web 回答、SQL 查询与行数展示、统一引用和刷新后历史投影；这不是在线服务或真实数据库验收。

演示顺序：知识库问题 → SQL 汇总 → 直接读 URL → SQL＋网页混合问题 → 点击 SQL/网页引用 → 刷新历史确认 → 未配置 SQL/非法网址的错误说明。

## 后续

首期多 Agent 不扩建父子块、精排、文档版本、独立 Worker、可恢复 Run/Event、多租户及模型中心。后续已在独立阶段接入 [Excel 行级来源](excel-row-citations.md)，沿用本页的 Supervisor 与专职 Agent。

后续检索增强：[父子上下文检索](parent-context-retrieval.md)，保留本页 Supervisor/子 Agent 分工。
