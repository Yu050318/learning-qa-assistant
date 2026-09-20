# 多用户 RAG 后端 V1 设计

## 1. 目标与范围

第一版实现一个纯后端、多用户、固定检索的知识库问答服务。用户能够上传 PDF、Markdown、TXT、DOC 和 DOCX 文档，创建会话并基于自己的文档进行多轮问答。

技术栈：

- Python 与 FastAPI
- LangChain Python
- Milvus 向量数据库
- 云端 PostgreSQL
- 千问 API Embedding
- DeepSeek 与本地 Ollama 对话模型

第一版不开发 Vue 前端、不实现注册登录、不使用自主决策 Agent、不引入任务队列、重排序或混合检索。客户端暂时通过 `X-User-ID` 传递用户标识；这只提供开发期逻辑隔离，不构成安全认证。

## 2. 架构原则

系统采用模块化单体：物理上是一个 FastAPI 服务，逻辑上分为接口、应用编排、领域接口、基础设施适配和工作流五层。

```text
app/
├── api/                 # HTTP 路由、请求与响应模型
├── application/         # chat 与 ingestion 用例编排
├── domain/              # Retriever、Memory、LLM 等抽象接口
├── infrastructure/      # 外部系统适配器
│   ├── llms/            # DeepSeek、Ollama
│   ├── embeddings/      # 千问 Embedding
│   ├── vectorstores/    # Milvus
│   ├── persistence/     # PostgreSQL
│   └── loaders/         # PDF、MD、TXT、DOC/DOCX
├── workflows/           # 固定 RAG 工作流
├── core/                # 配置、日志、异常
└── main.py
```

API 层不得直接依赖 Milvus、LangChain 具体链或模型 SDK。`RetrieverService` 对外提供稳定的检索接口，使 V2 能将其包装为 Agent Tool，只替换编排层。

## 3. 核心用例与数据流

### 3.1 文档入库

1. 请求携带 `X-User-ID` 和文件。
2. PostgreSQL 创建状态为 `processing` 的文档记录。
3. Loader 将文件统一解析为 LangChain `Document`。
4. 旧版 DOC 先通过有超时限制的 LibreOffice 子进程转为 DOCX。
5. 文本按标题、段落和句子边界清洗与切片；默认约 600–800 tokens，重叠 80–120 tokens，参数可配置。
6. 千问 API 批量生成 Embedding。
7. 向量、正文与引用元数据写入 Milvus。
8. 全部成功后，PostgreSQL 将文档改为 `ready`；失败则改为 `failed` 并保存错误原因。

每个 chunk 至少包含 `user_id`、`document_id`、`chunk_id`、顺序、文件名、页码或章节以及正文。

### 3.2 聊天问答

1. API 接收 `user_id`、`session_id`、问题、可选 `document_ids` 和模型提供商。
2. PostgreSQL 读取会话摘要与最近消息。
3. 工作流结合会话上下文将追问改写为独立查询。
4. 千问 Embedding 将查询向量化。
5. Milvus 强制按 `user_id` 检索，并按可选 `document_ids` 缩小范围；默认召回 `top_k = 6`。
6. 去除重复或高度重叠片段，构造受控 Prompt。
7. DeepSeek 或 Ollama 生成答案。
8. 服务端根据检索结果生成引用元数据。
9. PostgreSQL 保存用户消息、完整 AI 消息、实际模型、引用和 token 信息。
10. 未摘要消息达到配置阈值后更新滚动摘要。

V1 每次问答都执行检索，是带记忆的固定 RAG 工作流。V2 可把同一 `RetrieverService` 封装为工具，由 Agent 判断是否调用。

## 4. 存储职责

### 4.1 PostgreSQL

PostgreSQL 保存用户标识、文档元数据与处理状态、会话、原始消息、滚动摘要、引用和模型使用信息，不承担文档向量检索。

核心表：

#### users

- `id UUID PRIMARY KEY`
- `external_id VARCHAR UNIQUE NOT NULL`
- `created_at TIMESTAMPTZ NOT NULL`

#### documents

- `id UUID PRIMARY KEY`
- `user_id UUID NOT NULL REFERENCES users(id)`
- `original_name VARCHAR NOT NULL`
- `file_type VARCHAR NOT NULL`
- `file_path TEXT NOT NULL`
- `content_hash VARCHAR NOT NULL`
- `status VARCHAR NOT NULL`：`processing | ready | failed | deleting`
- `chunk_count INTEGER NOT NULL DEFAULT 0`
- `embedding_model VARCHAR NOT NULL`
- `error_message TEXT NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`
- 唯一约束 `(user_id, content_hash)`

#### chat_sessions

- `id UUID PRIMARY KEY`
- `user_id UUID NOT NULL REFERENCES users(id)`
- `title VARCHAR NOT NULL`
- `summary TEXT NOT NULL DEFAULT ''`
- `summarized_through UUID NULL`
- `created_at TIMESTAMPTZ NOT NULL`
- `updated_at TIMESTAMPTZ NOT NULL`

#### chat_messages

- `id UUID PRIMARY KEY`
- `session_id UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE`
- `role VARCHAR NOT NULL`：`user | assistant | system`
- `content TEXT NOT NULL`
- `model_provider VARCHAR NULL`
- `model_name VARCHAR NULL`
- `citations JSONB NOT NULL DEFAULT '[]'`
- `token_usage JSONB NULL`
- `created_at TIMESTAMPTZ NOT NULL`

删除会话采用硬删除。删除 `chat_sessions` 时，数据库级联删除其全部原始消息；摘要随会话一并删除。此操作不删除文档、原始文件或 Milvus 向量。

索引覆盖 `users(external_id)`、`documents(user_id, status)`、`chat_sessions(user_id, updated_at)` 和 `chat_messages(session_id, created_at)`。

### 4.2 Milvus

V1 使用共享 Collection，通过标量字段实现用户与文档过滤，不为每位用户创建独立 Collection。

字段包括：

- `chunk_id`：全局唯一主键
- `vector`：与选定千问 Embedding 模型维度一致
- `user_id`
- `document_id`
- `chunk_index`
- `content`
- `source_name`
- `page_number`（可空）
- `section`（可空）
- `embedding_model`

每次检索必须包含当前用户过滤条件。切换 Embedding 模型时必须新建兼容 Collection 或重新向量化全部文档，不能把不同向量空间混用。

### 4.3 原始文件

第一版原始文件保存在服务器受控目录，PostgreSQL 仅保存元数据和路径。服务端生成实际文件名，禁止把未经处理的用户文件名拼接为路径。

## 5. 跨存储一致性

PostgreSQL 与 Milvus 不能共享事务，因此采用状态机和幂等重试：

- 入库前创建 `processing` 记录。
- 使用确定性 chunk ID，或重试前按 `document_id` 清理旧向量。
- 只有向量全部写入后才能标记 `ready`。
- 查询只允许检索 `ready` 文档。
- 失败时记录错误并清理不完整向量。
- 删除时先标记 `deleting`，然后删除 Milvus 向量和原始文件，最后删除 PostgreSQL 记录。
- 所有写入和删除步骤必须可安全重复执行。

V1 可使用 FastAPI 后台任务，但入库逻辑必须封装在独立 `IngestionService`，为后续迁移到 Celery 或其他任务队列保留边界。

## 6. API 契约

所有接口使用 `/api/v1` 前缀并要求 `X-User-ID`。

### 文档

- `POST /documents`：上传文件，返回 `202 Accepted` 与文档 ID
- `GET /documents`：当前用户文档列表
- `GET /documents/{id}`：文档状态和错误原因
- `POST /documents/{id}/retry`：重试失败文档
- `DELETE /documents/{id}`：删除文档、向量和原始文件

### 会话

- `POST /sessions`：创建会话
- `GET /sessions`：会话列表
- `GET /sessions/{id}`：会话与消息
- `DELETE /sessions/{id}`：硬删除会话并级联删除消息
- `POST /sessions/{id}/messages`：非流式问答
- `POST /sessions/{id}/messages/stream`：SSE 流式问答

聊天请求包含 `question`、可选 `model_provider` 和可选 `document_ids`。响应包含消息 ID、答案、实际模型提供商和由服务端产生的引用列表。

资源不存在和资源不属于当前用户都返回 404，避免泄漏其他用户资源是否存在。

## 7. Prompt、引用与记忆

系统 Prompt 要求模型优先依据给定片段、资料不足时明确说明、不虚构来源，并将检索内容视为不可信数据而非系统指令。会话摘要仅用于理解上下文，不能作为知识事实来源。

引用编号和元数据由服务端根据本次检索结果生成；模型只能引用给定编号，不能自行生成文档名或页码。

PostgreSQL 永久保存原始消息，直到用户删除会话。运行时只注入旧摘要与最近若干轮消息。达到配置阈值（初始建议 10–20 条未摘要消息）后，用旧摘要和新增消息生成新摘要，并在同一数据库事务中更新 `summary` 与 `summarized_through`。摘要失败不能导致主要回答失败。

## 8. 模型适配与降级

`ChatModelProvider` 抽象同时提供完整生成与流式生成能力，具体实现为 `DeepSeekProvider` 和 `OllamaProvider`。

选择规则：

1. 优先使用请求显式指定的提供商。
2. 未指定时使用系统默认值，初始为 DeepSeek。
3. DeepSeek 超时、限流或服务异常时，可按配置降级到 Ollama。
4. Ollama 不可用时返回明确错误，不无限重试。
5. 响应和消息记录必须保存实际使用的提供商与模型。

Ollama 地址通过配置提供，部署环境必须能访问该地址。千问 Embedding 不自动切换到其他模型，以避免查询向量与已有文档向量不兼容。

## 9. 错误处理与安全

API 错误统一返回业务错误码、可读消息和 `request_id`。主要错误包括不支持的文件类型、无效文档、文档未就绪、资源不存在、Embedding 或 LLM 上游错误，以及数据库或向量库不可用。

聊天中先保存用户消息，完整生成成功后再保存 assistant 消息。SSE 中断不保存为正常完整回答；若保留则必须显式标记 `incomplete`。

安全要求：

- 所有资源查询同时约束内部 `user_id`。
- 校验扩展名、MIME、大小与空内容。
- 防止路径穿越；DOC 转换设置资源与时间限制。
- 密钥和连接串只通过环境变量或密钥管理提供。
- 云 PostgreSQL 使用 TLS。
- 日志不记录密钥、连接串、完整正文或完整聊天内容。
- LLM、Embedding、数据库、Milvus 与文件转换均设置超时。
- 服务暴露至不可信网络前必须增加真实认证或网关访问控制。

## 10. 日志、健康检查与测试

结构化日志至少记录 `request_id`、哈希后的用户标识、会话或文档 ID、操作、耗时、实际模型、检索数量和错误类型。耗时分解为记忆读取、查询改写、Embedding、检索、生成、记忆保存和总耗时。

健康检查：

- `GET /health/live`：进程存活
- `GET /health/ready`：PostgreSQL、Milvus 与必要配置可用

外部模型 API 不作为每次 readiness 的强依赖。

测试分层：

1. 单元测试：Loader 路由、清洗切片、Prompt、摘要边界、模型降级和用户过滤。
2. 集成测试：PostgreSQL 外键与级联删除、Milvus 写入/检索/过滤/删除、状态机与幂等重试。
3. API 测试：文档及会话 CRUD、非流式与 SSE 问答、跨用户访问和错误响应。
4. RAG 质量测试：固定问答集的 Top-K 命中、引用真实性、资料不足拒答和回答忠实度。

自动化测试使用模型与 Embedding 测试替身，只保留少量显式运行的真实供应商冒烟测试。

## 11. V1 验收标准

- 两个用户上传的文档、会话和消息彼此不可见且不可互相检索。
- 支持 PDF、Markdown、TXT、DOC、DOCX 入库并能查看处理状态。
- 重复执行失败任务不会产生重复 chunk。
- 问答每次固定检索 Milvus，并返回可核验引用。
- DeepSeek 与 Ollama 能通过统一接口选择，配置允许时支持有限降级。
- 会话能够保存完整消息，并在达到阈值后维护滚动摘要。
- 删除会话会硬删除其原始消息和摘要，但不影响文档。
- 删除文档会清理其 Milvus 向量与原始文件。
- 核心单元、集成和 API 测试通过，且有一组可重复的 RAG 质量基线。

## 12. 后续升级路径

V2 将 `RetrieverService` 包装为 LangChain Tool，让 Agent 自主决定是否检索；并可依次加入查询路由、重排序、混合检索、正式认证和任务队列。升级时保留现有 API、存储模型、Loader、Embedding、Retriever、Memory 与模型适配器，主要替换工作流编排层。
