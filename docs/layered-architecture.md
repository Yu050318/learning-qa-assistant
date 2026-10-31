# 分层架构与阿里规约适用清单

实施日期：2026-10-29。保留 Python/FastAPI、Vue、PostgreSQL、Milvus。依据[阿里巴巴官方规约仓库](https://github.com/alibaba/p3c)及[官方分层说明](https://alibaba.github.io/Alibaba-Java-Coding-Guidelines/#application-layers)，落实适用于本项目的工程职责；以下是本次改造的逐项范围，不代表全部 Java 条目通过认证。

## 目录与依赖

| 目录 | 职责 | 可以依赖 |
| --- | --- | --- |
| `frontend/` | Vue 交互、页面与展示 | HTTP 契约 |
| `app/open_api/` | 对外 HTTP 请求响应、现有错误与事件契约的发布入口 | entity、core |
| `app/web/` | 路由、认证上下文、基础校验、HTTP/SSE 编码 | open_api、service、装配入口 |
| `app/service/` | 权限决策、业务规则、业务流程、事务边界 | manager、dao、entity、core |
| `app/manager/` | 可复用能力、第三方适配、检索与运行协作 | dao、entity、core |
| `app/dao/` | SQL、ORM 驱动、Milvus、持久化查询 | entity、core |
| `app/entity/` | DO、DTO、BO、Query、VO 与纯模型运算 | core、其他 entity |
| `app/composition/` | 集中装配服务和资源生命周期 | 下层组件 |

Entity 是数据模型集合，不是请求必须经过的执行层。简单用例允许 Service 直接调用 DAO。Manager 只承载实际共用能力；不为每个 Service 创建空壳 Manager。开放接口层发布现有契约，Web 负责同一请求的 HTTP 适配，不增加一次转发。

DO 在受控事务内交给 Service 更新业务状态；SQL、查询构建与数据库会话的实际调用留在 DAO。Service 使用 `manager.transactions.Transaction` 明确提交/回滚边界。DAO 不自行提交跨 DAO 事务。响应通过 VO/Pydantic 契约序列化，禁止返回 ORM 的内部字典。`entity/query/document_tasks.py` 是纯查询条件对象，不能装入 SQLAlchemy Select 或 Session。

`app/main.py` 和根目录 worker/CLI 是进程入口。`app/bootstrap.py` 保持原有命令语义；依赖装配位于 `composition`，避免包与该 CLI 同名。数据库迁移仍由 Alembic 显式执行。

## 模块入口

| 业务 | Service | Manager / DAO |
| --- | --- | --- |
| 认证、组织与权限 | `auth`、`sharing`、`workspaces`、`capabilities` | `dao/postgres/repository.py`、`dao/queries/` |
| 文档、版本、索引发布 | `ingestion`、`document_tasks`、`document_publication` | `manager/loaders/`、`manager/hierarchy.py`、`dao/milvus.py` |
| 对话与持久运行 | `chat`、`runs`、`service/agent/` | `manager/agent_runtime.py`、`run_checkpoint.py`、`run_events.py` |
| 个人记忆 | `memories`、`automatic_memory` | `manager/openviking.py`、`dao/postgres/automatic_memory.py` |
| 评测与模型管理 | `evaluation*`、`model_center`、`memory_evaluation` | provider、HTTP 适配与评测 DAO |

原 `app.api`、`app.application`、`app.domain`、`app.infrastructure`、`app.workflows` 中的旧模块保留严格的 `sys.modules` 转发，以兼容旧导入和测试替身。它们不含第二份实现。新代码必须使用规范路径；对应关系见 [module map](validation/architecture-module-map.json)。后续移除兼容入口前应先核对外部调用方。

## 规约适用清单

| 规约主题 | 适用方式 | 实现与验证 |
| --- | --- | --- |
| 分层职责、上层依赖下层 | 适用；允许跨过简单中间层 | `scripts/check_architecture.py` 检查方向、循环、相对导入与包入口 |
| 开放接口、Web、Service、Manager、DAO 职责 | 适用 | Web 不访问 DAO/ORM/向量库/模型 SDK；SQL 查询在 DAO 中构造和执行 |
| 对象区分 DO/DTO/BO/Query/VO | 适用；复用已有有效类型 | entity 子目录、Query 纯条件、OpenAPI 兼容性测试 |
| 命名清楚、避免无意义编号 | 适用；采用 Python `snake_case` | DAO 用业务名称；包名小写；不套用 Java `lowerCamelCase` |
| 参数校验、权限隔离、默认安全 | 适用 | Pydantic、认证依赖、owner 查询、团队/评测隔离与 URI 归属校验 |
| 数据库参数化、索引、幂等 | 通用原则适用；方言适配 PostgreSQL | SQLAlchemy 参数绑定、唯一约束、队列索引、租约与去重键 |
| 事务边界、资源关闭 | 适用 | Service 决定提交；网络/模型工作在长事务之外；资源由装配及 worker finally 关闭 |
| 并发控制、任务补偿 | 适用 | 保留原 Run 检查点、advisory lock、outbox、幂等；自动记忆追加持久任务、代际隔离与租约 fencing |
| 异常与日志 | 适用；沿用错误码/request ID | 统一 HTTP 边界记录；新适配器保留原因链；日志只含稳定错误码/类型与资源 ID，不输出密钥或正文 |
| 测试与兼容 | 适用 | 原 HTTP schema 精确对比、SSE/权限/恢复等原测试、真实 PostgreSQL 小范围集成测试 |
| Java 语法、JVM、Java 集合、Spring/MyBatis/P3C 插件 | 不适用 | 不宣称 Python 通过 Java P3C |
| MySQL 专属类型、引擎、排序规则及方言 | 不直接套用 | 保持 PostgreSQL，执行适用的数据约束和事务原则 |

本次重点是分层边界与新增记忆流程。历史业务并未逐条按整本手册重新命名、重写；兼容字段名和已有协议保持原样。静态检查也不能证明所有业务权限和并发语义正确，因此与行为回归一起验收。

## 兼容性与可复核指标

改造前的静态快照为 [architecture-baseline.json](validation/architecture-baseline.json)：Web 直接访问持久化的文件为 1 个，原 application 中直接导入 SQLAlchemy 的文件为 20 个。后者包含如今归入 Manager 的共用模块，不能将两个不同目录口径说成相同 Service 集合的前后值。

当前门禁要求：规范 Web 直接依赖持久化为 0；规范 Service 直接导入 SQLAlchemy 为 0；禁止依赖与循环为 0。机器结果见 [验收报告](validation/architecture-openviking-acceptance.json)。这是结构改进指标，不能等价为吞吐或开发效率提高。

```powershell
.venv/Scripts/python.exe scripts/check_architecture.py
.venv/Scripts/python.exe -m pytest -q
cd frontend
npm.cmd run build
```

旧 HTTP path 和 component 的基线保存在 `validation/architecture-openapi-baseline.json`，`tests/test_architecture.py` 逐项比较。SSE 顺序、重放、幂等、租约与权限继续使用既有回归测试；原 80 题真实模型回归独立记录在[实测交付](validation/memory-live-20261029/report.md)，不以单元测试代替实际质量门禁。

自动记忆需要新增的三张表由迁移 `0011_automatic_memory` 单独创建；其余分层调整没有主动修改原表定义。启用说明见 [OpenViking 个人记忆](openviking-memory.md)。
