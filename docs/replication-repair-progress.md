# 功能补齐进度

参考基线：本地 SuperMew `eb0495e`。[原始差异清单](replication-gap-audit.md)保留修复前的核查快照，当前状态以本文为准。沿用当前 FastAPI/Vue/PostgreSQL/Milvus，保留历史数据，按功能与流程对齐，不逐屏复制参考主题。每阶段验收后先提交并推送 GitHub，再进入下一阶段。

| 阶段 | 交付范围 | 状态 |
| --- | --- | --- |
| 1 | Alembic 基线与升级、管理员邀请码、持久角色、鉴权与导航 | 已验收并推送 e4d58d1 |
| 2 | 部署预检、安全启用已有能力、独立入库 Worker、旧文档补建 | 已验收并推送 dd7221c |
| 3 | Run Worker、检查点、取消/HITL、租约/fencing、outbox | 已验收并应用本地；准备提交与推送 |
| 4 | 版本化模型 Profile、四种角色、Step-back/HyDE | 未开始 |
| 5 | 数据集/任务/结果、Evaluation Worker、12 文档/80 题真实 A/B | 未开始 |
| 6 | 租户与检索范围、候选构建/核验/发布、退休与统一任务 | 未开始 |
| 7 | Skill/Tool 注册与版本、能力中心、命令面板 | 未开始 |
| 8 | Run-bound 审批、独立 Sandbox、产物、完整追踪界面 | 未开始 |
| 9 | 共享限流、安全头、契约生成、监控和全量验收 | 未开始 |

阶段完成只说明对应范围已验收，不表示全部功能已复现。团队空间的全局切换与隔离不能由参考侧边栏装饰性“知识空间”卡片推定已存在；第六阶段按实际租户与知识库能力实施，当前仍只有组织共享基础。

## 阶段一验收（2026-10-17）

- 后端离线全量：337 passed、22 skipped、38 subtests passed；新增的七项迁移集成默认显式跳过，另行实测。
- 独立 PostgreSQL 18：迁移专项 7 passed、模型中心真实并发与权限回归 1 passed，覆盖空库、重复升级、旧库无 stamp 接管与数据保留、结构不兼容回滚及禁止破坏性降级。
- 前端：`npm run build` 通过、`npm run test:agents` 8 passed；浏览器实测邀请码注册管理员→管理导航→管理页面→刷新身份恢复→退出→普通注册无管理入口→直接访问 `/admin` 被重定向。
- 角色/API 测试覆盖错误与轮换邀请码、不允许客户端指定 role、注册关闭、邀请尝试限制、旧 Token 降权、白名单不再授予权限、显式白名单迁移的原子性和幂等性。
- 测试使用独立临时 PostgreSQL 容器及独立测试账号，未升级已有业务库、未修改本地 `.env`、未调用付费模型。
- 页面截图：[阶段一管理员界面](screenshots/platform-admin-stage1.png)。功能状态均为真实测试配置，未用演示数字冒充运行状态。

说明：目前实现的是管理员基础页面；概念图中的完整运行总览、模型角色、评测工作台等随对应阶段交付。

## 阶段二验收（2026-10-17）

- 本地环境先备份数据库、`.env`、上传及解析目录至忽略目录 `data/backups/20261017-130928-stage2`；数据库归档已通过 pg_restore 目录读取检查。随后将六张旧基础表升级至当前迁移 head，初始化模型中心，启用六项已有能力并切换 durable 入库。没有新增管理员账号或自动提升原账号；邀请码仅保存在本地 `.env`，需通过注册创建管理员。
- 本地 API 重启、独立入库 Worker 启动后，`/health/ready` 的数据库、向量、模型配置、Worker 全部为 ok。[实际预检与补建结果](validation/stage2-deployment.json)：两份历史 ready 文档均补建成功，共 1478 个层级节点；保留原叶子 ID 与向量，没有重新调用 embedding。
- 新增 Worker 心跳表与队列 kind 迁移；旧文档补建复用现有持久队列、锁和租约。失败仅改变任务状态，不破坏 ready 文档。预检覆盖迁移版本、目录、历史文件路径、Milvus 结构、认证/模型配置、管理员与匹配的 Worker 心跳。
- 最终后端离线全量：343 passed、23 skipped、38 subtests passed。真实独立 PostgreSQL：迁移、Worker 与模型中心共 10 passed，包含 Worker 心跳过期使 HTTP readiness 返回 503、恢复心跳后返回 200。
- 发现并修复测试隔离缺陷：pymilvus 导入时会隐式 load_dotenv，使 `_env_file=None` 的测试仍继承工作区开关。测试环境禁用依赖的隐式加载，新增回归验证显式 Pydantic dotenv 读取仍有效；未通过关掉真实功能来掩盖测试失败。
- 前端类型检查与构建通过，逻辑测试 8 passed。Docker 后端和前端镜像均实际构建；Python/uv、Node、Nginx 使用官方发布源并锁定已验证 digest。
- 独立 Compose 验证环境实际运行 API、Nginx 前端及入库 Worker，API/Worker 健康，使用专属测试数据库和共享测试卷；[容器预检结果](validation/stage2-docker-preflight.json)通过。没有把当前业务目录挂载进测试容器。
- 浏览器实测容器环境邀请码注册、模型入口、管理员能力状态和只读部署预检；[界面截图](screenshots/deployment-preflight-stage2.png)。容器模型凭据为测试占位值，预检只验证配置，不据此声称真实生成通过；本阶段没有调用付费模型。
- Rerank 保持关闭：当前无 endpoint/key 配置，界面和预检明确显示未启用。规划/Grader 已启用，后续真实问答会增加有预算限制的模型调用。
- 依赖审计另发现 DOMPurify low、source-map-js high 两项可修复告警，列入第九阶段治理验收，当前不宣称依赖审计全部通过。

## 阶段三验收（2026-10-17）

- 已增加并迁移 Run 所有权、generation/fencing、检查点、调用结果凭据和事件 outbox 表；保存已完成调用的结果，未确认的有副作用调用禁止自动重做，只读调用恢复次数有界。
- 已增加原子准备入口：同一事务冻结历史、检索文档范围、模型配置和预算，并创建唯一的用户/助手消息对；同键重试不重复准备，准备失败整体回滚。配置快照采用显式字段白名单，不保存密钥与数据库连接串。
- 同一个 Run 的 waiting/resume 已接入冻结执行器、正式 API 和前端，覆盖一次性恢复标识、重复提交幂等、过期拒绝、累计执行预算和取消竞态。
- 已实现 PostgreSQL 通知唤醒与 SSE 轮询兜底，事件日志仍为事实来源。LISTEN 连接关闭后丢弃，不将 autocommit 设置带回业务事务池。
- API 创建 Run 已切换为原子准备后入队；同步问答、兼容流式入口和显式 Run 接口共用持久执行，API 不再运行问答线程池。每用户待处理上限由数据库用户行锁串行核对，满额时仍可用原幂等键查询结果。独立进程入口为 `python -m app.run_worker`，`CHAT_RUN_WORKERS` 控制进程内执行槽，完整 Compose 增加 run-worker；部署预检/readiness 分别核验入库与 Run Worker。
- 当前业务环境先备份至忽略目录 `data/backups/20261017-180807-stage3`，pg_dump 自定义归档通过 pg_restore 目录检查，另保存 `.env`、上传和解析文件及 SHA256；随后迁移至 `0004_durable_run_control`，启动隐藏后台 API、前端、独立入库 Worker 和 Run Worker。[本地部署预检](validation/stage3-deployment.json)及 HTTP readiness 全通过；保留原用户、两份文档和 1478 个层级节点，没有改写向量，也未在业务环境请求收费生成。
- 新增冻结快照执行入口与 RunWorker 执行槽：沿用原用户/助手消息，模型和历史按快照消费，每个调用边界及发布前复核身份、权限与租约；等待检查点、助手内容和事件在同一事务提交。Worker 的草稿写入带 fencing，取消后不能发布成功结果；会话锁仍被旧执行占用时延后领取且不重置累计预算。
- 新增故障回归实际运行 Supervisor：模拟最终事务提交前崩溃，租约到期后恢复原 Run；五次已完成模型调用和一次 SQL 查询均未重复执行，最终仍只有原消息对。Planner/Grader、检索、模型回退及多委派顺序也覆盖接管复用。最终离线全量 **374 passed、27 skipped、38 subtests passed**，测试线程异常视为失败。
- 实际 PostgreSQL 迁移/通知/并发/独立进程专项 **12 passed**；另旧 Run 锁与并发事件序列集成 **1 passed**。验证并发提交只创建一条 Run、竞争领取只有一个 owner、接管后旧 owner 拒绝写入；独立 Run Worker 子进程健康检查不能冒用其他实例心跳。测试使用独立库，没有读取业务账号或调用收费模型。
- 前端已提供停止执行、人工补充、刷新后恢复等待状态；复用原助手气泡。SSE 将历史暂停标为非当前，避免恢复后的重放误停。类型检查/Vite 构建及 **13 项 Node 测试**通过。浏览器实际验证等待→刷新→补充→同一 Run 完成，以及运行中点击停止→cancelled；[消息和事件证据](validation/stage3-browser-runs.json)确认恢复后仍为原消息对。[等待界面](screenshots/run-hitl-stage3.png)、[恢复完成](screenshots/run-hitl-resumed-stage3.png)、[取消结果](screenshots/run-cancelled-stage3.png)均为独立真实 PostgreSQL/API/Run Worker 配合本地脚本模型的截图，不冒充真实上游生成。
- Docker 后端和前端镜像实际构建；独立测试栈运行 API、Nginx 前端及两个 Worker，[预检](validation/stage3-docker-preflight.json)全通过。[Worker 停启与排队取消](validation/stage3-docker-worker-cycle.json)验证 Worker 停止后 readiness=503、恢复后=200，停机时入队后取消的任务不被执行。测试容器、共享测试卷、临时进程和专属数据库已清理，当前业务服务保留运行。
- 修正两处测试隔离缺陷：图执行不能共享版本测试中外部绑定的单条 SQLite Connection；多图线程也不能依赖会回收其他线程连接的 SingletonThreadPool。相关执行测试使用独立连接与持久临时文件，真实锁语义由 PostgreSQL 单独验证。保留知识澄清不携带事实引用的约束，允许检索后继续询问用户。
