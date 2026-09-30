# LangchainDemo 项目架构分析

## 一、项目整体架构

项目采用了类似 **Hexagonal / Clean Architecture** 的分层设计：

```
┌──────────────────────────────────────────────────────────┐
│  api (入口层)            routes.py / dependencies.py       │
├──────────────────────────────────────────────────────────┤
│  application (应用层)    chat.py / sessions.py / ...     │
├──────────────────────────────────────────────────────────┤
│  workflows (业务流程)    rag.py / agent.py               │
├──────────────────────────────────────────────────────────┤
│  domain (领域契约)       contracts.py (Protocol + DTO)   │
├──────────────────────────────────────────────────────────┤
│  infrastructure (基础设施)  llms/ / vectorstores/ / ...  │
└──────────────────────────────────────────────────────────┘
```

依赖方向从上到下：`api → application → workflows → domain → infrastructure`

---

## 二、启动流程

应用入口在 `app/main.py`，通过 FastAPI 的 lifespan 启动钩子完成所有组件的初始化。

```
app/main.py:create_app()
  │
  ├── get_settings()                        # app/core/config.py — 读取 .env 配置
  │
  └── Services(settings)                    # app/application/container.py — 依赖注入容器
      │
      ├── Database(settings)                # 创建 PostgreSQL 引擎
      ├── MilvusVectorStore(settings)       # 创建 Milvus 连接
      ├── QwenEmbedding(settings)           # 创建 Embedding 客户端
      ├── ModelRouter(...)                  # 路由到 DeepSeekProvider / OllamaProvider
      ├── RetrieverService(embeddings, vectors, top_k)
      ├── FixedRAGWorkflow(retriever, models)              # v1 工作流
      ├── AgentRAGWorkflow(settings, retriever, models, web_search)  # v2 Agent 工作流
      ├── IngestionService(...)
      ├── SessionService(database)
      └── ChatService(settings, database, sessions, workflow, agent_workflow)
```

`Services` 类 (`app/application/container.py:22`) 是整个项目的**依赖注入容器**，负责把基础设施层的实例组装起来。

启动后 `services.start()` 会调用 `ingestion.recover_pending()` 恢复上次中断的文档处理任务。

---

## 三、用户问答完整流程（v1 版本）

假设前端调用 `POST /api/v1/sessions/{session_id}/messages`，中间经历如下调用链：

### 3.1 API 入口层

```
routes.py:chat()                          # 路由处理函数
  │
  ├── services = ServiceDependency        # → dependencies.py:get_services()
  │   从 request.app.state.services 取到启动时注入的容器
  │
  ├── user_id  = UserDependency           # → dependencies.py:current_user()
  │   根据请求头 X-User-ID 在数据库中查找或创建用户
  │
  └── services.chat.answer(              # ← 进入应用层
         user_id, session_id,
         payload.question,
         payload.document_ids,
         payload.model_provider
     )
```

### 3.2 应用层 ChatService

`app/application/chat.py:52` 的 `answer` 方法通过**两次 DB 会话**完成整个问答事务：

```python
def answer(self, user_id, session_id, question, document_ids, provider):
    # ---- 第一阶段：写入用户消息 ----
    with self.sessions.exclusive(session_id):           # 1. 获取会话互斥锁
        with self.database.sessions() as database:      # 2. 开启 DB 会话
            repository = Repository(database)
            session = repository.chat_session(...)      # 3. 查询 ChatSession
            ready_ids = repository.ready_document_ids(...)  # 4. 查询可用文档
            history = repository.messages(...)           # 5. 查询历史消息
            session.summary                             # 6. 获取会话摘要
            database.add(ChatMessage(role="user", ...)) # 7. 先把用户问题存进去
            database.commit()

        # ---- 第二阶段：调用工作流 ----
        answer, citations, retrieved_count = self.workflow.run(
            user_id, question, ready_ids, history, summary, provider
        )

        # ---- 第三阶段：写入 AI 回答 ----
        with self.database.sessions() as database:      # 8. 第二轮 DB 会话
            # 校验引用文档在回答期间没被删除
            database.add(ChatMessage(role="assistant", ...))  # 9. 存 AI 回答
            database.commit()
            return message
```

`SessionService.exclusive()` (`app/application/sessions.py:17`) 使用 `threading.Lock` 保证同一 session 串行处理。

### 3.3 FixedRAGWorkflow（v1 工作流）

`app/workflows/rag.py:23`，经典 RAG 三步：

```
FixedRAGWorkflow.run()
  │
  ├── 步骤 1: Query Rewrite（有历史时）
  │   └── self.models.generate([...])
  │       └── ModelRouter.generate()              # app/infrastructure/llms/providers.py:101
  │           └── DeepSeekProvider.generate() / OllamaProvider.generate()
  │               └── LangChainProvider.generate()
  │                   └── self.client.invoke(convert_messages(messages))
  │
  ├── 步骤 2: Retrieval
  │   └── self.retriever.retrieve(user_id, query, document_ids)
  │       │
  │       ├── self.embeddings.embed_query(query)
  │       │   └── QwenEmbedding.embed_query()      # DashScope API
  │       │
  │       └── self.vectors.search(user_id, document_ids, vector, top_k*2)
  │           └── MilvusVectorStore.search()       # COSINE 相似度检索
  │
  └── 步骤 3: Generation with Citations
      └── self.models.generate([SystemPrompt + History + Evidence + Question])
          └── 同步骤 1 的 LLM 调用链

最后用正则提取模型输出中的 [1][2] 引用编号，构造 citations 返回。
```

### 3.4 v1 完整调用链总结

```
HTTP Request
  │
  ▼
middleware.py:BodyLimitMiddleware.__call__()        # 限制请求体大小
  │
  ▼
main.py:request_context() middleware                 # 分配 request_id、计时
  │
  ▼
dependencies.py:current_user()                       # 根据 X-User-ID 查/建用户
  │
  ▼
routes.py:chat()                                     # 路由处理
  │
  ▼
chat.py:ChatService.answer()
  ├── sessions.exclusive()                           # 会话锁
  ├── Repository.chat_session()                      # 查 ChatSession
  ├── Repository.ready_document_ids()                # 查可用文档
  ├── Repository.messages()                          # 查历史消息
  ├── DB.add(ChatMessage user) → commit             # 存用户问题
  │
  ├── workflows/rag.py:FixedRAGWorkflow.run()
  │   ├── (可选) ModelRouter.generate() → Query Rewrite
  │   ├── QwenEmbedding.embed_query()                # 问题 → 向量
  │   ├── MilvusVectorStore.search()                 # 向量检索
  │   └── ModelRouter.generate() → 带证据生成
  │
  └── DB.add(ChatMessage assistant) → commit         # 存 AI 回答
  │
  ▼
HTTP Response: ChatResponse
```

---

## 四、v2 Agent 版本流程

v2 通过 `POST /api/v2/sessions/{session_id}/messages` 或 `/messages/stream` 调用。

### 4.1 ChatService.answer_v2()

`app/application/chat.py:83`，与 v1 类似但增加了 `RunContext` 构建：

```python
def answer_v2(self, user_id, session_id, payload, request_id, on_answer_event=None):
    public_query, web_enabled, warnings = prepare_public_query(payload, ...)
    # 同样的 exclusive 锁 + DB 读写用户消息
    context = RunContext(                              # app/domain/contracts.py:76
        request_id, user_id, session_id, question,
        search_mode, allowed_document_ids, selected_provider,
        public_query, web_enabled, warnings, deadline_monotonic
    )
    result = self.agent_workflow.run(context, history, summary, on_answer_event)
    # 存 assistant 消息 + run_metadata
```

### 4.2 AgentRAGWorkflow.run() 核心逻辑

`app/workflows/agent.py:250`：

```
AgentRAGWorkflow.run()
  │
  ├── EvidenceRegistry(budget)                        # 管理所有证据的编号和 token 预算
  │
  ├── 定义两个工具函数 (闭包):
  │   ├── search_knowledge(query)                       # → RetrieverService.retrieve()
  │   └── search_web()                                # → TavilySearch.search()
  │
  ├── ModelRuntime (AgentMiddleware)                  # 模型调用中间件
  │   ├── 超时 / 重试 / 上下文预算控制
  │   └── wrap_model_call() 支持 fallback
  │
  ├── create_agent(model, tools, system_prompt, middleware=[runtime])
  │   └── LangGraph Agent 实例
  │
  ├── agent.invoke() 或 agent.stream()                # LangGraph 执行
  │   │
  │   ├── 当 Agent 决定调用 search_knowledge:
  │   │   └── RetrieverService.retrieve()
  │   │       └── EvidenceRegistry.add_knowledge(hits) # 登记证据编号
  │   │
  │   └── 当 Agent 决定调用 search_web:
  │       └── TavilySearch.search(query)
  │           └── EvidenceRegistry.add_web(batch)
  │
  ├── 策略性补充检索 (policy_retrieval):
  │   ├── Agent 没用任何工具且 search_mode 允许 → 自动 search_knowledge
  │   └── auto 模式且知识库分数低于阈值 → 自动 search_web
  │
  ├── 解析 FinalAnswer JSON → EvidenceRegistry.validate()
  │   └── 校验引用编号合法性
  │
  └── AgentRunResult(ModelAnswer, kind, citations, search_mode, run_metadata)
```

### 4.3 流式响应 SSE

对于 `routes.py:stream_chat_v2()`：

```
stream_chat_v2()
  │
  ├── 启动后台 Thread 调 services.chat.answer_v2(on_answer_event=lambda ...)
  │
  └── events() generator 通过 Queue 消费事件:
      ├── "run_started"
      ├── "answer_start" / "answer_delta"
      │   └── AnswerStreamParser.feed()                # agent.py:39
      ├── "context_usage"
      ├── "usage" (token usage)
      └── "done" (完整消息)
```

`AnswerStreamParser` 从 JSON 格式的模型输出中增量提取 `"answer"` 字段的内容，实现打字机效果。

---

## 五、其他关键业务流程

### 5.1 文档上传与异步处理

```
routes.py:upload_document()
  │
  ├── services.ingestion.upload()                     # ingestion.py:138
  │   ├── validate_upload() / validate_document_container()
  │   ├── hashlib.sha256 计算 digest
  │   └── Repository.duplicate() → DocumentRecord(status="processing")
  │
  └── services.ingestion.schedule()                    # ingestion.py:58 — ThreadPoolExecutor
      └── _run_scheduled() → process()                 # ingestion.py:219
          ├── (MinerUParser.parse() 或 LocalDocumentLoader.load())
          ├── _save_cache / _load_cache                # 解析结果缓存
          ├── loader.chunks(...)                        # 切片
          ├── QwenEmbedding.embed_documents(batch)     # 批量向量化
          └── MilvusVectorStore.upsert(batch, vectors)  # 入库
```

### 5.2 会话上下文压缩

```
routes.py:compact_context()
  │
  └── chat.py:ChatService.compact()
      ├── sessions.exclusive()
      ├── Repository.chat_session() + messages()
      ├── count_tokens(old_summary, conversation)       # evidence.py:19 — tiktoken 估算
      ├── self.agent_workflow.models.generate([...])    # LLM 生成摘要
      └── DB 更新 session.summary + session.summarized_through
```

压缩后 `Repository.context_messages()` (`repository.py:95`) 会跳过 `summarized_through` 之前的消息，直接从摘要之后开始取上下文。

---

## 六、各模块职责一览

| 文件 | 职责 | 关键类 / 函数 |
|------|------|--------------|
| `main.py` | FastAPI 应用工厂、生命周期、全局中间件/异常处理 | `create_app()`, `request_context` |
| `bootstrap.py` | 显式初始化 PostgreSQL schema / Milvus collection | `main()` |
| `routes.py` | 所有 REST API 端点定义 | `chat()`, `chat_v2()`, `stream_chat_v2()` 等 |
| `dependencies.py` | FastAPI 依赖注入（Services, User） | `get_services()`, `current_user()` |
| `middleware.py` | 请求体大小限制 | `BodyLimitMiddleware` |
| `container.py` | **依赖注入容器**，组装所有服务 | `Services` |
| `chat.py` | 问答应用服务层（事务编排、DB 读写、防并发） | `ChatService.answer()`, `answer_v2()` |
| `sessions.py` | 会话 CRUD + 互斥锁 | `SessionService` |
| `ingestion.py` | 文档上传、异步处理流水线 | `IngestionService` |
| `retriever.py` | 向量检索（封装 Embedding + VectorStore） | `RetrieverService.retrieve()` |
| `evidence.py` | token 计数、证据注册表、引用校验 | `EvidenceRegistry`, `count_tokens()` |
| `rag.py` | **v1 工作流**: Query Rewrite → Retrieval → Generation | `FixedRAGWorkflow.run()` |
| `agent.py` | **v2 工作流**: LangGraph Agent + 工具 + 策略性检索 | `AgentRAGWorkflow.run()` |
| `contracts.py` | 领域契约 (Protocol 接口 + dataclass DTO) | `EmbeddingProvider`, `VectorStore`, `RunContext` |
| `repository.py` | 纯 SQLAlchemy ORM 查询层 | `Repository` |
| `database.py` | SQLAlchemy engine / session 管理 | `Database` |
| `providers.py` | LLM 适配层 (DeepSeek/Ollama + Router + Fallback) | `ModelRouter`, `LangChainProvider` |
| `qwen.py` | DashScope Embedding API 客户端 | `QwenEmbedding` |
| `milvus.py` | Milvus 向量库客户端 | `MilvusVectorStore` |
| `tavily.py` | Tavily 联网搜索客户端 | `TavilySearch` |

---

## 七、核心类依赖关系图

```
                        ┌────────────┐
                        │  routes.py │
                        └─────┬──────┘
                              │ 调用
                        ┌─────▼──────┐
                        │ ChatService│ ←───────────────────────┐
                        └─┬──┬───┬───┘                          │
                          │  │   │                              │
           ┌──────────────┘  │   └──────────────┐               │
           ▼                 ▼                   ▼               │
    SessionService    IngestionService    (workflow.run)        │
           │                 │                                   │
           ▼                 ▼                                   │
       Repository     LocalDocumentLoader                       │
       (SQLAlchemy)   MinerUParser                              │
                      QwenEmbedding    ◄────────┐               │
                      MilvusVectorStore ◄───────┤               │
                                                │               │
                                   ┌────────────┴──────┐        │
                                   │ RetrieverService  │        │
                                   └────────┬──────────┘        │
                                            │ 被调用            │
                           ┌────────────────┼──────────┐       │
                           ▼                ▼          ▼       │
                    FixedRAGWorkflow  AgentRAGWorkflow ────────┘
                           │                │    │
                           │                │    └── TavilySearch
                           │                │
                           └───────┬────────┘
                                   ▼
                           ModelRouter
                             │      │
                             ▼      ▼
                    DeepSeekProvider  OllamaProvider
```

---

## 八、设计亮点

1. **Protocol 接口解耦** — `contracts.py` 用 Python Protocol 定义了 `EmbeddingProvider`、`VectorStore`、`ChatModelProvider` 等抽象接口，基础设施层各自实现，上层依赖接口而非具体实现，方便替换和测试。

2. **依赖注入容器** — `Services` 类集中组装所有组件，测试时可以轻松替换为 mock 实现。

3. **事务模式** — `ChatService` 用两次 DB session（写入用户消息 → 工作流执行 → 写入 AI 回答），配合 `SessionService.exclusive()` 的 `threading.Lock` 保证同一 session 串行处理，防止并发冲突。

4. **证据注册表** — `EvidenceRegistry` 集中管理知识库和网页两种证据的编号、token 预算、去重和引用校验，v1 工作流直接在 `FixedRAGWorkflow` 内部处理，v2 统一交给 `EvidenceRegistry` 更规范。

5. **Agent 策略性补充检索** — Agent 没调用工具或分数不够时，由工作流自动触发检索，不依赖 Agent 模型完全自主决策，避免模型"忘记"搜索。

6. **模型自动降级** — `ModelRouter` 和 `ModelRuntime` 都支持从 DeepSeek 自动 fallback 到 Ollama，提高可用性。

7. **流式输出解析** — `AnswerStreamParser` 从模型输出的 JSON 格式中增量提取 `"answer"` 字段，实现打字机效果同时保持 JSON 协议的结构化优势。