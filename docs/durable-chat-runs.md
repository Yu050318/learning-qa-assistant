# 第九阶段：Run/Event 持久化与 SSE 断线重放

> 本文保留最初实现路线的历史说明。后续补齐阶段三正在将 API 线程池改为独立 Run Worker，并加入检查点、取消与同一 Run 的人工恢复；新协议和边界见[持久运行执行说明](durable-run-execution.md)，交付状态以[功能补齐进度](replication-repair-progress.md)为准。

本阶段让流式问答先保存公开事件，再通过 SSE 发送。网络断开后只读取已有事件，复用原 Supervisor、SQL/Web/RAG Agent、预算、引用校验、ChatService 和消息表，不重新执行问题。默认 `CHAT_RUNS_ENABLED=false`，保持旧接口行为。

## 调用链与复用位置

1. 前端为一次发送生成 UUID 请求标识，调用 `POST /api/v2/sessions/{id}/runs`，放入 `Idempotency-Key` 请求头。服务端校验会话归属，保存 Run 和首条 run_started 后返回 202。
2. 同一个用户和请求标识只对应一个 Run。重复请求的会话、问题、模式、文档范围、模型或 URL 不同时返回 `IDEMPOTENCY_CONFLICT`；相同请求返回原 Run，不增加用户消息或模型调用。数据库还限制同一会话最多一个 running Run。
3. 问答仍由原 `ChatService.answer_v2()` 调用 Supervisor，在 API 进程的有界线程池中执行。与第八阶段负责文档入库的独立 Worker 分开；本阶段不持久化或自动重跑问答执行任务。默认每 API 进程最多 4 个执行槽，满额返回 429，原 Run 查询与重连不占执行槽。
4. 原 Agent 状态、answer_start 和回答片段通过同一回调落入 `rag.chat_run_events`，序号在持有 Run 行锁时分配；每次提交后才可被 SSE 读取。首片立即保存，后续小片段按 512 字符或约 200ms 合并，后续事件或最终保存前刷新尾片，避免每个 token 都提交数据库事务。
5. 原引用校验、版本快照与权限检查继续执行。助手消息、context_usage/usage/done、Run succeeded 和 message_id **在同一事务提交**。客户端始终以 done 中的最终答案和来源替换临时回答。保存失败时不会留下“已有助手消息、却没有最终事件”的半成功状态。
6. 前端 GET 事件流，重连提交 `Last-Event-ID: <run_id>:<sequence>`，也支持 `?after=N`。后端仅返回更大的序号；前端再去重并检查序号连续性。传输丢失最多重试 3 次，创建请求重试始终复用原 key，终态错误和权限错误不会触发重新执行。
7. 刷新或重新打开会话时，前端查询最近 Run，正在执行的问答从序号 0 重放到新的临时助手气泡，最终重新加载原消息历史；成功结果使用已保存的消息，不插入重复用户消息。

核心文件：`app/application/runs.py` 负责状态与事件，`app/application/chat.py` 负责原消息事务，`app/api/routes.py` 提供创建/查询/重放接口，`frontend/src/sse.ts` 负责解析与去重，`frontend/src/api.ts` 和 `store.ts` 负责原 key 重试及恢复。没有新增依赖或多 Agent 框架。

## 显式启用

```powershell
# 只为已有平台库增加两张表；不修改原消息或向量
.venv/Scripts/python.exe -m app.bootstrap --chat-runs

# 在服务端 .env 设置后，重启所有 API 进程：
# CHAT_RUNS_ENABLED=true
# CHAT_RUN_WORKERS=4
```

前端通过 `/api/v2/capabilities` 的 `features.durable_runs` 自动选择新链路。原 `POST /api/v2/sessions/{id}/messages/stream` 也保留：启用后创建或取回 Run 并订阅事件流，可传 UUID Idempotency-Key；未传时服务端生成新 key，因此该兼容调用的调用者不能盲目重试 POST。非流式 `/messages` 继续使用原同步路径和返回结构，本阶段的 Run 持久化范围是流式/显式 Run 问答。

多个 API 进程必须连接同一个 PostgreSQL。会话处理、压缩、重命名和删除沿用 SessionService，并在启用本特性时增加跨进程 session advisory lock。它需要 PostgreSQL 直连或 session pooling，不能使用事务级 PgBouncer。事件重放可连接其他 API 进程；每批最多 100 条，空闲时约每 500ms 查询一次，服务端发送 keep-alive。

正常退出等待已接受的问答完成后关闭连接。回退前先停止新请求并等待在途任务，再设置 `CHAT_RUNS_ENABLED=false`、重启全部 API。两张表可以保留；关闭时不提供历史 Run 重放，但原消息和引用仍可读。没有自动清库或迁移。

## 接口

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST | `/api/v2/sessions/{id}/runs` | 要求 UUID Idempotency-Key；创建或复用 Run，返回 202 |
| GET | `/api/v2/sessions/{id}/runs` | 当前用户会话最近 20 个 Run |
| GET | `/api/v2/runs/{run_id}` | 状态、最后序号、最终消息 ID 和到期时间 |
| GET | `/api/v2/runs/{run_id}/events` | 从 Last-Event-ID 或 after 游标订阅、重放 |
| POST | `/api/v2/sessions/{id}/messages/stream` | 保留旧入口，启用后读取持久化事件 |

Run 状态为 running、succeeded、failed、interrupted。未知 Run、跨用户 Run/会话统一返回 404。负数、超过已保存序号或属于其他 Run 的游标返回 422；终态最后序号之后的订阅直接结束，不生成新事件。请求标识不是认证凭据，所有查询和重连仍要求有效 Bearer token，前端沿用原 token 刷新逻辑。

单个事件最多 2MB，常规事件累计最多 6MB/8192 条，终态预留到总计 8MB/8200 条。超限会记录 `RUN_EVENT_LIMIT` 并结束，助手消息不提交。保存内容仅为原公开 SSE 事件，包括最终答案、引用摘录、SQL 来源卡和执行状态；不保存模型原始提示、完整检索候选或 SQL 连接串。事件包含私有资料的输出，与聊天记录采用相同的访问控制和保留边界；本阶段没有自动 TTL 清理，显式删除会话会通过外键级联删除 Run/Event。

## 中断边界

浏览器断线不会停止已经接受的问答；问答执行与 SSE 订阅分离。Run 从接收时起使用 `AGENT_TIMEOUT + 30秒` 的固定到期时间。API 被强制结束、数据库暂时不可用或执行超时后，读取/重放或下一次创建会检查过期，并持久化 RUN_INTERRUPTED 错误；没有后台定时器，不应把尚未访问的旧 running 行解释为仍有执行进程。

中断后不会再次调用 SQL、爬虫或收费模型。相同 key 仍返回原中断 Run，需要用户明确发起新问题才创建新的 Run。迟到的执行通过 Run 状态和到期检查拒绝追加事件或保存助手消息；底层已发出的 HTTP/工具调用不能被撤回，仍依赖原超时和预算。本特性提供的是**持久化回放和幂等提交**，不提供模型执行 checkpoint、自动进程接管、exactly-once 外部调用或跨设备实时通知。

用户消息在原 ChatService 的开始事务中保存，执行失败时可能只有用户消息和 Run 错误，刷新后不将草稿当作最终助手回答。若进程在 Run 创建后、用户消息提交前退出，Run 仍能显示中断状态，但不会伪造已执行的问答。若服务已经接受请求而客户端多次丢失创建响应，刷新会话可发现原 running Run；不要通过重新点击发送来模拟网络重连。

## 验证与演示

```powershell
.venv/Scripts/python.exe -m pytest tests/test_chat_runs.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m app.bootstrap --help
cd frontend
npm run test:agents
npm run build
```

后端离线测试使用真实 ChatService、SQLite 持久化和替身工作流；另有真实 Supervisor → 脚本模型 → SQL 替身 → ChatService → Run/Event 的调用链测试，验证 SQL 引用重放。覆盖消息与终态事务回滚、幂等、容量上限、过期后迟到结果、事件预算、跨用户、游标、兼容流式入口、不同读取实例和会话删除。前端测试实际调用 API 客户端，通过 fetch 替身模拟创建响应丢失、部分流中断、重复事件、UTF-8 拆包与终态错误，不调用收费服务。

真实 PostgreSQL 行锁、会话锁与并发序号分配单独显式验证：

```powershell
# 仅使用专用隔离库，数据库名必须包含 test
$env:RUN_CHAT_RUN_INTEGRATION='1'
$env:CHAT_RUN_TEST_DATABASE_URL='postgresql+psycopg://TEST_ROLE:TEST_PASSWORD@localhost/rag_runs_test?sslmode=require'
.venv/Scripts/python.exe -m pytest tests/integration/test_chat_runs.py -q
```

本次没有执行真实表升级、PostgreSQL 锁集成、真实模型/Tavily/Milvus 或浏览器断网验收，不能将离线结果当作线上通过。手动演示应先在独立环境启用配置，再发送 SQL＋Web 问题、短暂断网、恢复后查看引用和历史；强制重启 API 后等待 Run 到期，应看到明确中断，而不是自动重做工具调用。

后续仍有租户与共享权限、模型中心和完整三级分块；独立入库 Worker 继续沿用第八阶段实现。

2026-10-16 验证记录：后端全量 **285 passed、12 skipped、38 subtests passed**；前端 vue-tsc/Vite 构建及 **8 项 Node 测试**通过；bootstrap help、`uv lock --check`、diff 空白检查通过。19 个变更源码/文档文件未命中当前配置的长密钥值。第八阶段 `6fef592` 已推送 GitHub main；本阶段新增代码尚未提交或推送，真实数据库和收费服务未执行升级/验证。
