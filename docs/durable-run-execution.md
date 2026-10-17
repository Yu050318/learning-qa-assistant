# 持久 Run 执行与人工恢复

本文描述已验收的功能补齐第三阶段。当前本地业务环境已备份升级并通过就绪检查；提交与推送状态见[补齐进度](replication-repair-progress.md)。

API 接收请求时在同一事务保存 Run、冻结历史/模型配置/文档范围、用户消息、助手占位和首事件。相同幂等键复用原 Run，不增加消息。独立 `python -m app.run_worker` 领取任务，API 只负责入队、查询和订阅。`CHAT_RUN_QUEUE_LIMIT` 是每个用户的待处理上限，包含运行、等待人工输入和取消中的任务；`CHAT_RUN_WORKERS` 是每个 Worker 进程的并行槽数。

## 状态与接口

| 接口 | 行为 |
| --- | --- |
| POST `/api/v2/sessions/{id}/runs` | UUID `Idempotency-Key` 必填，原子准备后返回 202 |
| GET `/api/v2/runs/{id}` | 状态、消息 ID、事件序号；waiting 时还返回 question、checkpoint_revision、resume_token、expires_at 和暂停事件序号 |
| POST `/api/v2/runs/{id}/cancel` | queued/waiting 直接取消；running 进入 cancelling，由当前 owner 或到期接管者确认 cancelled；重复调用幂等 |
| POST `/api/v2/runs/{id}/resume` | 请求体为 checkpoint_revision、resume_token、supplement；恢复原 Run、原助手消息，返回 202 |
| GET `/api/v2/runs/{id}/events` | 支持 Last-Event-ID/after；断开订阅不会取消运行 |

状态为 queued、running、waiting、cancelling、cancelled、succeeded、failed、interrupted。同一会话只能存在一个非终态 Run。所有操作均复核用户身份和归属，跨用户返回 404。查询和事件响应不缓存。

恢复标识只能消费一次；相同标识、相同修订和相同补充内容可重复提交，修改补充内容则返回冲突。补充不能改变原模型、文档或工具权限。等待时间不消耗执行预算；重新领取和恢复都沿用累计预算。超时的人工等待由 Worker 扫描或读操作持久化为 interrupted。

原同步 `/messages` 在启用 Run 时通过相同队列等待执行结果，遇到澄清时返回原助手消息及 waiting 元数据；等待超过响应预算则返回 202 和 Run 查询入口。兼容 `/messages/stream` 同样只入队并订阅。未启用 Run 时保留原执行路径。

## 检查点与接管

Worker 使用数据库时钟领取租约，以 owner token 和递增 generation 限制每次事件写入、调用凭据和最终提交。心跳延长租约；租约失效后旧执行者不能发布结果。会话 advisory lock 尚被旧进程占用时延后重试，累计预算不重置。

每个模型/工具边界保存稳定输入指纹和结果凭据。恢复通过已确认结果重建本地图，不重复已完成调用。多工具调用按模型给出的顺序提交，保持证据编号一致；嵌套 Agent 不受全局单线程执行器限制。人工暂停还保存完整对话和共享运行计数，下一轮只追加补充输入。

无法确认是否完成的只读调用最多尝试三次；无法确认的副作用调用标记 uncertain，禁止自动重复。因此不承诺外部调用 exactly-once，已发送的网络请求也不能被撤回。取消在下一处边界生效，底层请求仍受自身超时限制。

助手内容、等待检查点/终态事件和 Run 状态在同一事务提交。消息顺序由显式创建时间保证。错误或取消保留原助手占位并记录状态，不将草稿当作成功答案。恢复、每次调用以及最终发布前重新核对冻结文档的访问权限。

事件与 outbox 同事务保存。PostgreSQL NOTIFY 只携带 Run ID/序号，SSE 从数据库读取事件；通知丢失时轮询兜底。历史 run_waiting 带 `is_current=false`，客户端不会把它误认为当前暂停。浏览器刷新复用已有助手消息，补充后从暂停序号继续读取。

检查点包含私有上下文和工具结果，按聊天数据的归属保护；配置采用白名单，不持久化凭据或平台数据库连接串。删除会话级联删除 Run、事件、检查点和调用凭据。

## 部署和验收

先备份、停止旧执行器、执行 `python -m app.migrate`，再启动匹配配置的 API、入库 Worker 和 Run Worker。完整部署使用 `compose.yaml` 加 `compose.full.yaml`；仅启用 `CHAT_RUNS_ENABLED` 而未启动匹配 Run Worker 时 readiness 不通过。Worker 支持 `--once` 和 `--healthcheck`，容器健康检查绑定本进程实例 ID，不能借用其他进程心跳。

验收需同时包括离线回归、真实 PostgreSQL 的并发与接管、真实独立进程、浏览器停止/人工恢复/刷新、容器运行和当前业务环境升级。现有源码测试覆盖并不代表这些交付门槛已全部完成。
