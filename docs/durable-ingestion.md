# 第八阶段：独立、可恢复的文档入库 Worker

本阶段复用现有 `IngestionService` 的解析、规范化缓存、分块、Embedding、Milvus、版本和用户校验。新增 PostgreSQL 任务表及一个独立 Python 进程入口，没有增加队列依赖。默认仍是 `INGESTION_MODE=in_process`，现有安装无需升级即可继续使用。

## 调用链与复用位置

1. 原 `/api/v2/documents` 或版本上传接口保存原始文件，在**同一个数据库事务**内创建 Document、版本绑定及 `rag.ingestion_jobs`。成功后仍返回 202；持久化模式下 `schedule()` 不启动 API 内线程。
2. `python -m app.worker` 使用短事务 `FOR UPDATE SKIP LOCKED` 领取到期任务，生成新的随机 claim token、计数并设置租约。初版每个 Worker 顺序处理一个文档，需要增加并发时启动多个进程。
3. Worker 取得每文档 PostgreSQL session advisory lock，再调用原 `IngestionService.process()`。解析、缓存、Embedding 和向量写入前后检查租约与锁连接，心跳每租约周期的三分之一续期。
4. 成功写入 ready 后确认任务 succeeded。若进程在 ready 提交后、任务确认前退出，接管者保留 ready 并补充确认，不重新解析；即使次数已耗尽也优先确认已经成功的文档。
5. 暂时故障采用 5、10、20 秒等指数退避，上限 300 秒，总尝试次数默认 3。崩溃后的接管也计入次数；耗尽后标为 failed，必须从原重试接口手动重置。遇到另一个进程仍持有文档锁时回队等待，不消耗执行次数。

代码入口：`app/application/ingestion_jobs.py` 负责持久化状态，`app/worker.py` 负责领取/心跳/执行，`app/application/ingestion.py` 保留实际入库，`app/infrastructure/persistence/database.py` 管理跨进程锁。所有数据库时间判断使用 PostgreSQL `clock_timestamp()`，避免长事务起始时间影响租约判断。

## 显式启用

先备份配置，确认 API 与 Worker 使用同一个平台 PostgreSQL，并能以**同样的绝对路径**访问上传、解析缓存目录。多台机器需要共享持久卷；当前不提供对象存储。停止旧版入库入口并等待进程内任务结束后再切换，避免旧进程绕过新的跨进程锁。

```powershell
# 仅添加任务表；不会清库、重建向量或改写文档
.venv/Scripts/python.exe -m app.bootstrap --ingestion-jobs

# 在 API、Worker 共用的服务端 .env 中设置：
# INGESTION_MODE=durable
# INGESTION_LEASE_SECONDS=120
# INGESTION_MAX_ATTEMPTS=3
# INGESTION_POLL_SECONDS=2

# 重启 API，再在独立终端或进程管理器中启动：
.venv/Scripts/python.exe -m app.worker

# 运维时只执行一次领取（最多一个任务，不等待退避到期）：
.venv/Scripts/python.exe -m app.worker --once
```

Worker 不自动建表，启动时检查已有平台表。每轮最多补建 100 个没有任务记录的旧 processing 文档，已有任务不会被恢复逻辑重置。API 崩溃在 202 返回前后不会丢失已经提交的任务；文件写完但事务尚未提交时进程被强杀，可能留下没有记录的源文件，本阶段没有自动扫盘清理功能。

`INGESTION_WORKERS` 只控制旧模式线程数。持久化模式按 Worker 进程数扩容，每进程还会使用心跳线程。每个正在执行的 Worker 通常占用锁连接、工作会话和短暂的心跳/校验连接，需结合 PostgreSQL 总连接数配置进程数。session advisory lock 要求直连 PostgreSQL 或 session pooling，**不支持事务级连接池/事务模式 PgBouncer**。

SIGINT/SIGTERM 停止领取新任务，当前任务等待既有解析和 HTTP 超时结束；强制结束依靠租约过期恢复。`--once` 返回 0 只表示一次领取流程完成或没有到期任务，文档是否成功以任务状态为准。进程初始化/轮询失败返回 1。生产环境应由操作系统进程管理器重启 Worker。

回退时先停止并等待所有 Worker，再将所有 API 改回 `in_process`；原线程恢复逻辑会接管 processing 文档。任务表可保留，期间的任务状态不再更新；重新启用 durable 后需要核对旧终态任务与文档状态，不能将切换模式作为自动修复工具。

## 故障与数据边界

Embedding 的网络错误、HTTP 429/5xx允许自动重试，鉴权、配置和响应格式错误不自动重试。MinerU 仅对明确的限流、上游不可用、解析超时、下载超时/失败进行自动重试；`MINERU_SUBMISSION_UNKNOWN` 不自动重提。Milvus 当前只有统一不可用错误码，因此该错误有限重试，不能据此区分服务故障与凭据错误。原始文件哈希被改动则直接失败。

沿用 MinerU batch_id 和规范化缓存恢复，上传中断时沿用原逻辑创建新的 attempt/generation；进程被强杀时无法确认云端是否已经接受上传，因此不承诺避免所有重复云解析费用。文档 UUID 不变、重建前删除其旧向量，使用原 chunk 身份写入。重试和删除共用同一把文档锁；删除记录通过外键级联删除任务。版本文件、历史引用和其他用户的文档保持独立。

这是 **at-least-once** 入库。随机 token 拦截旧拥有者的续期/确认，advisory lock 阻止正常数据库连接下同一文档被两个进程同时处理。但 PostgreSQL 与 Milvus/文件系统不在同一事务中：极端网络分区时，已发出的远端请求无法撤回，Milvus 也没有 token fencing，不能承诺 exactly-once 或完全杜绝迟到远端写入。若遇到此类故障，恢复连接后检查对应文档并手动重试重建。

## 状态与演示

前端沿用原资料列表和上传轮询的 processing/ready/failed 状态，无新增管理页。原 `GET /api/v2/documents/{id}/processing` 增加可空 `task`：

```json
{"state":"queued","attempts":1,"max_attempts":3,"available_at":"2026-10-16T08:00:05Z","last_error_code":"EMBEDDING_UNAVAILABLE"}
```

完整任务状态为 queued/running/succeeded/failed/cancelled。只有文档所属用户可以查询；响应和结构化日志不包含连接串、claim token、MinerU 批次号或原始异常。退避期间文档仍为 processing，phase 为 retry_wait。没有 Worker 运行时任务保持 queued，这不等于入库成功。

演示：启用配置并启动 API → 上传 Markdown → 查询 processing.task 确认 queued → 启动 Worker → 文档 ready/任务 succeeded → 使用原知识库问答验证引用。恢复演示可在**独立测试环境**处理较大文件时强制结束 Worker，再启动另一个进程并等待租约；不要在真实资料上为演示注入错误。

## 验证

```powershell
.venv/Scripts/python.exe -m pytest tests/test_ingestion_worker.py -q
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m app.worker --help
.venv/Scripts/python.exe -m app.bootstrap --help

# 仅使用专用隔离测试库，数据库名必须含 test；不可填写真实平台库
$env:RUN_WORKER_INTEGRATION='1'
$env:WORKER_TEST_DATABASE_URL='postgresql+psycopg://TEST_ROLE:TEST_PASSWORD@localhost/rag_worker_test?sslmode=require'
.venv/Scripts/python.exe -m pytest tests/integration/test_ingestion_worker.py -q
```

离线专项使用 SQLite、替身 Embedding/向量和可控时钟，覆盖事务回滚、领取与过期接管、旧 token 拒绝、ready 未确认恢复、崩溃耗尽、退避和手动重试、源哈希、删除、跨用户 API 校验及 HTTP 错误分类。SQLite 不证明 PostgreSQL 并发正确性；单独的 PostgreSQL 用例验证 SKIP LOCKED、session advisory lock、租约过期与任务确认，要求专用且没有其他任务的测试库。本次未执行真实数据库升级、真实 PostgreSQL/Milvus/Worker 集成或付费服务调用。

后续阶段：持久化 Run/Event 与 SSE 断线重放，再考虑租户共享权限、模型中心及完整三级分块。

2026-10-16 验证记录：最终后端全量 **274 passed、11 skipped、38 subtests passed**；Worker、版本与 MinerU 专项 **59 passed**。Worker/bootstrap help、`uv lock --check`、diff 空白检查通过；15 个变更源码/文档文件未命中当前已配置的长密钥值。本阶段未修改前端，沿用现有上传轮询。本阶段新增代码保留在本地，尚未提交或推送；此前第七阶段 `3d28f0d` 已成功推送至 GitHub main。
