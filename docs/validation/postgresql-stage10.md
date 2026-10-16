# PostgreSQL 联合预验收：2026-10-16

在实现第十阶段后发现本机 Docker 已启动，因此进行了第七至十阶段及 SQL Agent 的真实 PostgreSQL 预验收。本记录不代表全部项目验收完成。

## 环境与隔离

- Docker Server 29.5.3，使用本机已有 `postgres:18` 镜像，没有替换现有服务。
- 新建随机名称容器 `codex-rag-pg-e66381ec`，只绑定 `127.0.0.1` 的随机宿主端口，数据库 `rag_platform_test`。
- 容器内使用 tmpfs，无已有数据库卷或主机资料挂载；512MB 内存、1 CPU、32MB shared_buffers、最多 40 连接。
- 临时密码随机生成，仅传递给本次进程和容器，不记录或提交连接串。测试进程中的 `DATABASE_REQUIRE_TLS=false` 仅用于这一本机临时数据库，不修改项目 .env 或真实平台库配置。
- 测试结束在 finally 中停止带 `--rm` 的容器，输出确认清理完成。没有连接现有 PostgreSQL、修改生产 schema、清理现有数据或发送模型请求。

## 实际执行

设置 `RUN_VERSION_INTEGRATION / RUN_WORKER_INTEGRATION / RUN_CHAT_RUN_INTEGRATION / RUN_SHARING_INTEGRATION / RUN_SQL_INTEGRATION=1`，相应测试 URL 指向上述隔离服务器，执行：

```powershell
.venv/Scripts/python.exe -m pytest `
  tests/integration/test_document_versions.py `
  tests/integration/test_ingestion_worker.py `
  tests/integration/test_chat_runs.py `
  tests/integration/test_sharing.py `
  tests/integration/test_sql_readonly.py -q
```

结果：**8 passed in 6.15s**，进程退出码 0。临时容器已清理。

## 证明范围

| 测试 | 验证内容 |
| --- | --- |
| DocumentVersion 1 项 | 同族版本并发锁、旧当前指针冲突拒绝 |
| IngestionWorker 1 项 | SKIP LOCKED、不等待另一个任务行锁、会话 advisory lock、过期租约拒绝续期、旧 token 拒绝确认、ready 后确认 |
| ChatRun 1 项 | 不同数据库连接的会话锁互斥、相同 key 复用、8 个并发片段的连续序号、终态后拒绝追加 |
| Sharing 1 项 | 最终授权快照阻止另一个连接撤权，事务结束后撤销生效并拒绝新检索范围 |
| SQLReadonly 4 项 | 获准 schema、聚合、百分号字面量、结果截断、写入/系统表拒绝、查询超时、具有 UPDATE 权限的账号被拒绝 |

没有验证真实 Embedding、模型、联网搜索、Milvus 混合检索、MinerU、浏览器断线或多账号界面行为；这些仍需最终联合验收。上述集成用例仅使用自建记录和隔离库，不能将通过结果扩大为真实部署已经完成。
