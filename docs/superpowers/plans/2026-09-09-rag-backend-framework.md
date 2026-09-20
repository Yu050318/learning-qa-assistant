# RAG Backend Framework Implementation Plan

**Goal:** 在当前工作区交付可启动、可联调的多用户固定 RAG 后端骨架。

**Architecture:** FastAPI 模块化单体；API 调用 application，工作流依赖领域协议，外部 SDK 仅出现在 infrastructure。外部服务按需连接，导入和启动不自动建库建表。

**Tech Stack:** Python 3.12、FastAPI、SQLAlchemy/psycopg、LangChain、pymilvus、httpx。

**Spec:** `2026-09-09-rag-backend-design.md`，以及用户确认的本轮骨架范围。

## Constraints

- 保留 `.env`、实验脚本和未提交修改，不创建分支或提交。
- 密钥不进入源码、模板、日志或验证输出。
- 非流式主链路；SSE 返回 501；滚动摘要与 MinerU 云解析后续实现。
- 所有资源查询限定内部用户 ID；只检索 ready 文档。
- 没有现有测试体系，不新增测试框架；使用临时离线冒烟验证。
- 数据库使用独立 `rag_v1` schema，显式初始化，不修改已有业务表。

## Task 1: 配置与基础设施

Files: `pyproject.toml`, `.gitignore`, `app/core/`, `app/domain/`, `app/infrastructure/`。

- [x] 环境变量别名、超时、脱敏日志、业务异常。
- [x] ORM、用户作用域仓储和显式初始化入口。
- [x] 本地 Loader、千问 Embedding、Milvus 和对话模型适配器。
- [x] 验证导入、配置、确定性 chunk ID 和过滤条件。

## Task 2: 应用主链路

Files: `app/application/`, `app/workflows/rag.py`, `app/api/`, `app/main.py`。

- [x] 上传、幂等入库、重试和可重试删除。
- [x] 会话 CRUD、最近消息、追问改写、检索和引用。
- [x] API、统一错误、健康检查；流式接口返回 501。
- [x] SQLite 与服务替身验证跨用户 404、入库和聊天。

## Task 3: 交付与验证

Files: `../../../.env`, `README.md`, `uv.lock`。

- [x] 安装依赖、更新锁文件、Windows 启动说明。
- [x] compileall、依赖一致性、HTTP 冒烟、只读连接检查。
- [x] 汇报服务状态、真实集成验证限制和后续范围。

## 验证记录（2026-09-09）

- 暂停前 46 项临时离线检查通过；恢复后重新执行 API 回归与适配器契约检查，均通过。
- 最终回归覆盖用户隔离、用户消息先保存、失败不保存 assistant、有限降级、引用编号、重试/删除补偿、级联删除、上传大小和异常脱敏。
- 千问批次/索引顺序/异常、Milvus 集合签名/租户过滤/确定性切片、本地 Loader、LangSmith 域名保护通过离线检查。
- 实际启动临时 Uvicorn 进程验证存活、Swagger/OpenAPI 和用户标识校验，结束后已停止该进程。
- 当前 readiness 为 503：PostgreSQL 配置触发 DATABASE_TLS_REQUIRED，Milvus 为 MILVUS_UNAVAILABLE；模型配置项已读取。
- PostgreSQL 的 TLS 只读连接未成功；未执行云端 DDL 或业务数据写入。没有调用付费模型，尚不能宣称真实 RAG 端到端验证通过。
- Ollama 可达且已有 qwen3.5:9b，默认值已对应；未执行真实生成。
- 独立审查子代理因额度限制未执行，最终采用本地代码检查与回归验证。
