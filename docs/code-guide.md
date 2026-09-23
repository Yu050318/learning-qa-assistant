# 多用户 RAG 后端：代码导读

> 面向读者：已经接触 Python，希望理解这个项目各模块如何协作。
> 编写日期：2026-09-09。说明依据当前源码，而不是把设计文档里的全部目标当成已实现功能。
> 本文所有源码路径均相对于项目根目录；示例用户和请求不包含真实凭据。

## 阅读导航

- 想先认识项目：阅读第 1～3 节。
- 正在看 `app/api/dependencies.py`：重点阅读第 4 节。
- 想跟着一次上传或问答看调用链：阅读第 5～6 节。
- 想理解数据库、向量库和 LangChain：阅读第 7～9 节。
- 想调试或继续开发：阅读第 10～14 节。
- 安装命令、环境配置和接口调用示例：配合根目录 `README.md` 使用。

---

## 1. 这个项目到底是什么

一句话：**用户上传自己的资料，服务先查资料，再让模型结合资料和最近对话回答问题。**

当前实现有两条主要业务链：

```text
资料准备：上传文件 → 提取文本 → 切片 → 生成向量 → 保存到 Milvus

聊天问答：接收问题 → 读取最近对话 → Agent 按模式调用知识库或网络工具
                       → 校验证据和引用 → 保存消息与运行元数据
```

当前由 `AgentRAGWorkflow` 约束模型和工具循环；知识性结论必须引用本轮登记的知识库或网页证据。

需要区分三件事：

| 概念 | 本项目中的具体含义 |
| --- | --- |
| 知识库 | 原始文档及其向量切片，作为回答的资料来源 |
| 会话记忆 | PostgreSQL 保存的对话；运行时主要取最近若干条帮助理解上下文 |
| 模型 | 负责问题改写和答案生成；文档向量由独立的 Embedding 接口生成 |

**上传文档不是训练模型。** 本代码没有模型训练或微调步骤；每次问答把检索出的片段放进请求消息中。

## 2. 先认识目录，不要从所有文件逐个看起

| 目录或文件 | 负责什么 | 最值得先看的入口 |
| --- | --- | --- |
| `app/main.py` | 创建应用、生命周期、中间件和异常处理 | `create_app()` |
| `app/api/` | HTTP 参数、依赖、路由和响应格式 | `dependencies.py`、`routes.py` |
| `app/application/` | 编排上传、聊天、会话等业务操作 | `container.py`、`chat.py`、`ingestion.py` |
| `app/domain/` | 模块间传递的数据结构与接口约定 | `contracts.py` |
| `app/infrastructure/` | 连接数据库、向量库、文件解析库和模型 SDK | 各子目录的适配器 |
| `app/workflows/` | 规定固定 RAG 的执行步骤 | `rag.py` |
| `app/core/` | 配置、日志、业务错误 | `config.py` |
| `app/bootstrap.py` | 显式初始化 PostgreSQL 表和 Milvus 集合 | `main()` |

可以按下面的方向理解调用关系：

```text
HTTP 请求
  → api：取参数，取得服务和当前用户
  → application：执行具体用例
  → workflow：问答时，组织改写、检索和生成
  → infrastructure：调用数据库、向量库、文件解析和模型接口
```

`domain/contracts.py` 提供这些模块交流时使用的对象和协议。

这里采用的是实用的分层单体，并不是所有依赖都严格倒置的“纯领域架构”：例如 application 中仍直接使用 SQLAlchemy 模型和 Repository。先理解实际代码，不必把每一层理解成只能依赖一个特定方向的形式化规则。

根目录的 `main.py` 和 `scau/agentdemo.py` 是保留的实验脚本，不是新后端的启动入口。

## 3. 服务启动时发生了什么

源码：`app/main.py`、`app/application/container.py`、`app/core/config.py`。

启动命令：

```powershell
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

其中 `app.main:app` 表示：加载 `app/main.py` 模块，并找到里面名为 `app` 的 FastAPI 对象。

启动分为两个阶段：

1. **导入模块。** 执行 `app = create_app()`，读取配置、创建 FastAPI 对象、注册中间件、异常处理器和路由。
2. **运行生命周期。** Uvicorn 启动应用时进入 `lifespan`，配置日志和追踪，创建并保存业务服务对象。

最关键的一行是：

```python
application.state.services = services or Services(settings)
```

正常启动时会创建 `Services(settings)`；检查代码时，也可以向 `create_app()` 传入组装好的替身服务。

### 3.1 Services 是什么

`Services` 是本项目手动编写的**服务装配对象**，不是 FastAPI 内置类，也不是额外引入的依赖注入框架。

它把各模块连接起来：

```text
Services
  ├─ database   → Database
  ├─ vectors    → MilvusVectorStore
  ├─ embeddings → QwenEmbedding
  ├─ models     → ModelRouter → DeepSeekProvider / OllamaProvider
  ├─ retriever  → RetrieverService(embeddings, vectors)
  ├─ agent_workflow → AgentRAGWorkflow(retriever, models, web_search)
  ├─ ingestion  → IngestionService(...)
  ├─ sessions   → SessionService(database)
  └─ chat       → ChatService(..., sessions, agent_workflow)
```

API 不需要在每个路由中重新创建数据库和模型对象，只需取得这个已经装配好的 `Services`。

### 3.2 创建对象不等于已连接服务

- `Database` 使用 `cached_property` 延迟创建 engine 和 sessionmaker。
- DeepSeek/Ollama 的客户端也是延迟创建的。
- Milvus 在适配器操作中建立客户端连接。
- 应用导入与启动不会自动建表，也不会自动创建向量集合。

因此，`/health/live` 可以正常返回，但业务所需的 PostgreSQL 或 Milvus 仍可能不可用。应用退出时，生命周期代码会调用 `services.close()` 释放已创建的数据库 engine。

## 4. 重点：dependencies.py 逐段解释

源码：`app/api/dependencies.py`。

这个文件只有两个核心任务：

1. 给路由提供 `Services`。
2. 把请求头里的外部用户标识转换成数据库中的内部用户 UUID。

### 4.1 先理解这里出现的类型

| 名称 | 在这段代码中的作用 |
| --- | --- |
| `Request` | 当前 HTTP 请求，可访问请求自身状态和所属应用 |
| `Annotated` | 为一个类型附加说明；这里附加 FastAPI 的依赖或参数来源信息 |
| `Depends` | 告诉 FastAPI：这个参数要通过指定函数取得 |
| `Header` | 告诉 FastAPI：这个参数来自请求头，并配置长度约束 |
| `UUID` | 当前用户在数据库中的内部标识类型 |
| `hashlib` | 为日志生成用户标识的摘要，避免直接记录原始标识 |

可以把 `Annotated[类型, 附加说明]` 理解为“这个值是什么类型，以及框架该如何取得或校验它”。**Annotated 本身不会执行函数；是 FastAPI 读取这些元数据来解析依赖。**

### 4.2 get_services：取出共享的服务对象

```python
def get_services(request: Request) -> Services:
    return request.app.state.services
```

把这一行拆开：

- `request`：本次请求。
- `request.app`：处理它的 FastAPI 应用。
- `request.app.state`：这个应用对象上保存自定义共享状态的位置。
- `services`：启动阶段存进去的 `Services` 实例。

该函数只取对象，不创建对象，也不直接执行数据库查询。

这里的“共享”是同一个应用实例、同一个进程里的共享，不代表多个 worker 进程共享同一个 Python 对象。

### 4.3 current_user 的参数来自哪里

以下节选参数声明，省略号表示暂时略去函数体；实际逻辑在下一小节展开。

```python
def current_user(
    request: Request,
    services: Annotated[Services, Depends(get_services)],
    x_user_id: Annotated[str, Header(min_length=1, max_length=200)],
) -> UUID:
    ...
```

三个参数有三种来源：

| 参数 | FastAPI 如何提供 |
| --- | --- |
| `request` | 注入当前 Request 对象 |
| `services` | 解析 `Depends(get_services)`，取得服务对象 |
| `x_user_id` | 从 HTTP 请求头读取字符串，并进行长度校验 |

`Header` 默认把参数名的下划线转换为连字符，所以客户端发送的是：

```http
X-User-ID: alice
```

不是请求体里的 `{"x_user_id": "alice"}`，也不是 URL 查询参数。

这里没有为 `x_user_id` 提供默认值，所以它是必填项。缺失时，请求会被参数校验拦截；本项目的异常处理器会把它转换成统一的 422 错误响应。

注意写法是 `Depends(get_services)`，不是 `Depends(get_services())`：传递的是“可供框架调用的函数”，而不是现在立即执行它。

### 4.4 current_user 的函数体做了什么

**第一步：去掉首尾空白。**

```python
identifier = x_user_id.strip()
```

例如 `" alice "` 变成 `"alice"`。代码没有把大小写统一，所以 `alice` 和 `Alice` 是不同的外部标识。

**第二步：拒绝空白标识和控制字符。**

```python
if not identifier or any(ord(character) < 32 for character in identifier):
    raise AppError("INVALID_USER_ID", "X-User-ID 不能为空或包含控制字符", 422)
```

这一层检查有必要：字符串 `"   "` 的原始长度不为零，但去掉空格后已经没有内容。`ord(character) < 32` 则检查该字符的码值是否落入代码所拒绝的控制字符范围。

**第三步：记录本次请求的日志标识。**

```python
request.state.user_hash = hashlib.sha256(identifier.encode()).hexdigest()[:16]
```

处理过程是：字符串转字节 → SHA-256 摘要 → 十六进制表示 → 取前 16 个字符。

它用于 HTTP 请求日志里的 `user_hash`，不是数据库主键、登录凭证或权限证明。截断的无盐摘要也不等于彻底匿名，尤其不能保护容易猜到的用户名。

**第四步：查询或创建用户，返回内部 UUID。**

```python
return services.user_id(identifier)
```

实际调用链为：

```text
current_user()
  → Services.user_id("alice")
  → 创建本次操作的 SQLAlchemy Session
  → Repository.user("alice")
       ├─ 已存在：返回 users 表里的记录
       └─ 不存在：创建 User，生成内部 UUID
  → 提交事务
  → 返回 user.id
```

因此，客户端不需要把 `X-User-ID` 写成 UUID。它可以发送 `alice`，然后由服务找到对应的内部 UUID。只有 URL 中的会话/文档 ID，以及请求里的 `document_ids` 等资源标识，才要求符合相应的 UUID 参数类型。

`Repository.user()` 中的唯一约束和嵌套事务用于处理并发创建相同外部用户的情况：如果另一请求已创建该记录，就重新查询它，而不是重复建立同一外部标识。

也因此，**业务接口即便是 GET，首次使用某个 X-User-ID 时也可能创建 users 记录**；解析当前用户需要 PostgreSQL 可用，并非纯粹读取请求头就结束。

### 4.5 request.state 与 request.app.state 不要混淆

| 表达式 | 生命周期/范围 | 本项目存放的东西 |
| --- | --- | --- |
| `request.app.state` | 同一个应用实例共享 | `services` |
| `request.state` | 当前请求 | `request_id`、`user_hash` |

如果把“当前用户”保存在 `app.state` 的一个共享变量中，请求之间就可能互相覆盖。当前代码把请求级信息放进 `request.state`，并把内部 `user_id` 作为参数传给业务函数。

### 4.6 最后两行是可复用的依赖别名

```python
ServiceDependency = Annotated[Services, Depends(get_services)]
UserDependency = Annotated[UUID, Depends(current_user)]
```

它们不是新的服务实例，也不是两个全局用户变量；只是给常用的类型与依赖声明起了名字。

所以路由可以简洁地写成：

```python
def create_session(payload: SessionCreate, services: ServiceDependency, user_id: UserDependency):
    return services.sessions.create(user_id, payload.title)
```

处理这个 HTTP 请求时，FastAPI 会先解析这些依赖，再调用路由函数。理解上可以把路由看作“接收已经准备好的服务对象和内部用户 ID，然后开始做业务”。

**不要把类型标注理解成普通 Python 函数调用时也会自动注入。** 如果你自己在脚本里直接调用函数，仍需要自己传入相应参数；注入发生在 FastAPI 处理请求的过程里。

### 4.7 这里为什么还不算认证

因为任何能访问接口的人都可以填写 `X-User-ID: alice`。代码没有验证密码、令牌签名或可信身份来源。

它提供的是开发阶段的逻辑用户隔离。以后增加真实认证时，可以优先替换 `current_user()` 的身份来源，让它从可信凭证中得到内部用户 ID，同时继续复用下面的仓储过滤逻辑。

## 5. 跟着一次文档上传阅读代码

主要源码：`app/api/routes.py` → `app/application/ingestion.py` → `app/infrastructure/loaders/local.py`。

### 5.1 请求阶段：先接收并保存文件

`POST /api/v2/documents` 对应 `upload_document()`。

执行顺序：

1. 通过依赖取得服务和当前用户 UUID。
2. `IngestionService.upload()` 校验文件名、扩展名、MIME 类型。
3. 服务端生成文档 UUID，使用它组成受控目录中的实际文件名。
4. 分块读取上传流，检查大小，同时计算内容 SHA-256。
5. 检查当前用户是否上传过相同内容。
6. 创建 `DocumentRecord`，状态设为 `processing` 并提交。
7. 路由把文档交给有界入库执行器，返回 202 和文档信息；应用重启时会恢复仍为 processing 的记录。

**202 只表示任务已接收，不代表文档已经能用于问答。** 客户端需要查询文档状态，等到 `ready`。

相同用户重复上传相同内容会返回 409，不是静默复用旧记录。不同用户允许分别上传相同内容，因为唯一约束是 `(user_id, content_hash)`。

### 5.2 后台阶段：再执行真正的入库

`IngestionService.process()` 会重新读取持久化的文件，不再依赖 HTTP 上传对象；所以路由可以关闭原始上传流。

```text
锁定当前用户的文档记录
  → 确认仍处于 processing
  → 清理该文档旧向量
  → Loader 提取文本
  → chunks() 清洗与切片
  → 分批生成 Embedding 并 upsert
  → 全部成功后更新 ready 和 chunk_count
```

这里的 `database.sessions()` 是为这次后台操作创建新的数据库会话，不是重用 HTTP 请求中一个已经关闭的数据库会话。

### 5.3 文件解析方式

| 格式 | 当前实现 | 元数据或限制 |
| --- | --- | --- |
| TXT | 读取 UTF-8/UTF-8 BOM 文本 | 纯文本，不提供页码 |
| Markdown | 按标题组织段落，保留文本 | 保存 section；不是完整 Markdown 语法树解析器 |
| PDF | pypdf 提取每页文本 | 页码从 1 开始；没有 OCR |
| DOCX | python-docx 提取段落和表格内容 | 没有排版引擎，不推算 Word 页码 |
| DOC | LibreOffice 临时转换成 DOCX 后解析 | 需要额外安装 LibreOffice，设置转换超时 |

`Loader.load()` 返回的是 LangChain 的 `Document`，其核心内容是 `page_content` 和 `metadata`。

随后 `chunks()` 使用文本切分器，默认约 700 tokens、重叠 100 tokens；这里不是“700 个汉字”，而是使用 `cl100k_base` 编码器计数的 token 窗口，不能当作每个供应商的精确计费 token 数。

切片的 `chunk_id` 由文档 UUID、切片序号和正文确定。对于同一个文档及相同的解析/切片结果，重复执行会得到相同 ID。

### 5.4 状态机与重试

```text
新上传 → processing → ready
                  └→ failed → retry → processing

已有文档 → deleting → 清理向量和原文件 → 删除数据库记录
                    └→ 清理失败：保留 deleting，可再次 DELETE
```

- 常规处理错误会记录为 `failed`，并尝试清理不完整向量。
- 如果整个进程退出或数据库事务失败，仍可能留下 `processing`；代码没有持久任务调度器来自动恢复。
- `retry()` 允许失败文档或中断的 processing 文档重试；正在持有行锁的任务会使竞争操作失败，而不是同时修改。
- 一个已经完成的文档再次进入 `process()` 时，会因不再是 processing 而跳过。

PostgreSQL 和 Milvus 没有共享事务。当前做法是“状态标记 + 确定性 ID + 幂等清理 + 重试”，不是跨两个数据库的原子提交。

## 6. 跟着一次问答阅读代码

主要源码：`app/application/chat.py`、`app/workflows/agent.py`、`app/application/retriever.py`。

### 6.1 ChatService 管持久化，Agent Workflow 管工具与回答

`ChatService.answer_v2()` 负责权限范围、消息持久化和会话并发控制。

`AgentRAGWorkflow.run()` 负责受控工具调用、上下文预算、模型降级、结构化输出和引用检查。

把两者分开，是为了以后替换问答策略时，不必同时重写 HTTP 接口和消息存储逻辑。

### 6.2 完整调用链

```text
POST /api/v2/sessions/{session_id}/messages
  → current_user()：取得内部用户 UUID
  → routes.chat_v2()：取得 V2ChatRequest
  → ChatService.answer_v2()
      1. 确认会话属于当前用户，取得会话锁
      2. 检查文档范围，只允许 ready 且模型兼容的文档
      3. 读取旧摘要与最近消息
      4. 保存本次 user 消息并提交
      5. AgentRAGWorkflow.run()
           → 按 knowledge / web / auto 暴露允许的工具
           → RetrieverService 或 Tavily 返回证据
           → EvidenceRegistry 编号、去重并限制预算
           → 模型生成结构化答案
           → 校验引用编号与来源类型
      6. 再检查被引用文档是否仍有效
      7. 保存 assistant 消息、实际模型、引用、用量和运行元数据
  → V2ChatResponse 返回给客户端
```

如果后续调用失败，之前已经提交的 user 消息仍保留，但不会保存一个伪装成成功的 assistant 消息。

### 6.3 模式与工具边界

`knowledge` 只允许私有知识库检索，`web` 只允许冻结后的公开查询，`auto` 先看知识库相关度再决定是否补充网页证据。工具和模型调用次数都受配置上限约束。

### 6.4 RetrieverService 不是模型

它是检索业务服务，主要完成：

1. 用 Embedding 接口把问题转换成向量。
2. 带着用户和文档范围向 Milvus 查询。
3. 检查返回结果仍属于允许的范围。
4. 去掉空正文，以及去除空白后完全重复的正文。
5. 保留最多 `top_k` 条结果。

默认最终 `top_k=6`，实际向量搜索最多先请求 `top_k * 2`，即 12 条，为去重留出余量。这里只是余量召回，不是重排序；也没有实现语义相近片段去重。

当前没有配置“低于某个相似度就一律拒答”的阈值，所以有检索结果并不代表这些结果足以支持答案，还需要提示词约束和后续质量评估。

### 6.5 Prompt 和引用分别由谁负责

`SYSTEM_PROMPT` 要求：只依据检索片段回答、资料不足要说明、把片段中的指令视为不可信数据、用给定编号引用。

服务端则负责生成引用元数据，包括文档 ID、切片 ID、文件名、页码或章节、正文摘录和分数。

模型答案里的 `[1]` 对应本轮第一个片段，不是数据库里的固定编号。最终响应只保留模型在答案中实际使用的有效编号；如果出现超出本轮范围的编号，就返回 `INVALID_CITATION`。

这不等价于“已经证明每句话都正确”：没有编号的回答不会仅因缺少引用而被这段校验拒绝，有效编号也不能自动证明事实忠实度。该功能主要防止返回不存在的引用编号。

如果检索为空，工作流直接返回资料不足提示，标记 `model_provider=none`、`model_name=retrieval-only`，不再调用答案生成模型。但前面的查询改写可能已经调用过模型。

当没有任何可用文档 ID 时，检索服务仍会生成查询向量，Milvus 适配器检查集合后返回空列表，不会去查询其他用户的全库数据。

## 7. 三种存储，各自负责什么

### 7.1 PostgreSQL：保存业务事实和对话

源码：`app/infrastructure/persistence/models.py`。

| 表/模型 | 主要职责 |
| --- | --- |
| `users / User` | 外部用户标识与内部 UUID 的映射 |
| `documents / DocumentRecord` | 文档归属、原文件路径、内容哈希、处理状态、切片数、模型信息 |
| `chat_sessions / ChatSession` | 会话归属、标题、摘要及摘要进度字段 |
| `chat_messages / ChatMessage` | 用户/助手原文、模型信息、引用和 token 用量 |

这些表放在 `rag` schema 下，而不是自动占用 public 下的同名表。

`Repository` 集中写查询条件，例如读取文档时同时限制 `document_id` 和 `user_id`。仅按文档 ID 查询是不够的，因为还要验证它属于当前用户。

`database.sessions()` 创建的是数据库操作上下文；`ChatSession` 表示聊天会话。它们都叫“Session”，但不是同一种对象。

### 7.2 Milvus：保存可检索的切片

源码：`app/infrastructure/vectorstores/milvus.py`。

保存的不是只有向量，还包括正文与引用所需的元数据。这样召回后可以直接拿片段构造 Prompt，并生成来源信息。

每次实际向量查询都包含类似以下含义的过滤条件：

```text
user_id == 当前内部用户UUID
AND document_id IN 本次允许检索的文档UUID列表
```

`user_filter()` 会把标识规范化为 UUID 字符串，再生成表达式，避免直接拼接任意用户输入。

集合描述还带有 `rag:模型名:维度` 签名。**相同维度不代表同一个向量空间**；代码会检查模型签名与向量维度，不能仅修改配置就继续使用不兼容的旧集合。

### 7.3 文件目录：保存原始资料

原始文件放在 `UPLOAD_DIR` 指定的受控目录中，默认是 `data/uploads`。服务器以 UUID 生成实际文件名，原始显示名称单独保存在元数据里。

### 7.4 删除会话与删除文档的区别

- 删除会话：数据库级联删除该会话的消息，摘要随会话一起消失；不删除知识库资料。
- 删除文档：清理向量、原文件和文档记录；不会自动擦除已经保存的历史聊天内容。
- 历史引用存放在消息 JSON 中，不是到 documents 表的外键；文档删除后，旧消息可能仍保留当时的引用记录，但不能据此认为原文件现在仍存在。

## 8. contracts.py 为什么定义这些对象和 Protocol

源码：`app/domain/contracts.py`。

| 对象 | 可以怎样理解 |
| --- | --- |
| `Chunk` | 一块正文，加上用户、文档和来源信息 |
| `SearchHit` | 一个 Chunk，加上检索得分 |
| `ChatTurn` | 一条发给模型的消息：角色和正文 |
| `ModelAnswer` | 模型输出：正文、实际提供商、模型名称、用量 |

这些 dataclass 避免上层业务到处读取供应商 SDK 的专有返回结构。`frozen=True` 限制对象字段重新赋值，但不代表字段中嵌套的字典也被深度冻结。

`EmbeddingProvider`、`VectorStore`、`ChatModelProvider` 则用 Protocol 描述“需要提供哪些方法”。例如检索服务需要一个能提供 `embed_query()` 的对象，不必把所有逻辑写死在某个具体客户端内部。

它们不是运行时自动验证器，也不是数据库表。当前具体适配器不必显式继承 Protocol；只要方法形状满足调用约定，就能作为依赖传入。离线验证时，用替身实现这些方法也是同一个思路。

## 9. LangChain、千问和模型路由分别在哪里

### 9.1 LangChain 在本项目中的实际使用点

- 文件解析统一成 `langchain_core.documents.Document`。
- 文本切片使用 `RecursiveCharacterTextSplitter`。
- 对话适配器将 `ChatTurn` 转换成 `HumanMessage / AIMessage / SystemMessage`。
- DeepSeek 和 Ollama 分别通过 `ChatDeepSeek`、`ChatOllama` 调用。

但固定 RAG 编排是自己写的 Python 方法，不是现成 Agent、LangGraph 图或 LCEL 链。Milvus 通过 pymilvus 直接操作；千问 Embedding 通过 httpx 请求兼容接口。

### 9.2 千问 Embedding 适配器

源码：`app/infrastructure/embeddings/qwen.py`。

`embed_documents()` 接受多个文本，按配置分批请求，再按返回的 index 排序，并检查数量、维度、有限数值和非零向量。

`embed_query()` 复用同一方法处理一条问题。文档和问题共用同一套模型配置，避免落入不同的向量空间。

默认模型是 `text-embedding-v4`、1024 维；这和原实验脚本中的 `qwen3-vl-embedding` 不是同一套设置。默认每批最多 10 条，具体值应以 `Settings` 和当前环境配置为准。

### 9.3 模型适配与有限降级

源码：`app/infrastructure/llms/providers.py`。

`DeepSeekProvider` 和 `OllamaProvider` 统一返回 `ModelAnswer`；上层不必分别处理两家 SDK 的返回对象。

`ModelRouter.generate()` 的规则是：

1. 请求指定了 provider，就优先使用它。
2. 否则使用配置的默认值，当前代码默认 DeepSeek。
3. 只有 DeepSeek 返回被标记为可重试的上游错误，且允许降级，才再调用一次 Ollama。
4. 其他情况直接报错，不无限重试，也不会自动替换 Embedding 模型。

“一次降级”是针对每次 `generate()` 调用而言。改写和答案生成分别调用路由器，所以整次 HTTP 问答不一定只有一次失败尝试。

响应和消息记录使用 `ModelAnswer` 中实际生成答案的 provider/model，而不是简单照抄请求参数。

## 10. 配置、数据模型和错误处理怎么看

### 10.1 Settings：集中读配置

源码：`app/core/config.py`。

- 从项目根目录的 `.env` 读取，并允许进程环境变量覆盖。
- `database_url` 兼容 `DATABASE_URL` 和 `DB_URL`。
- 密钥用 `SecretStr` 表示，正常展示对象时会遮蔽；真正请求前才取明文。它不是加密存储，仍不要手动打印 `get_secret_value()` 的结果。
- `get_settings()` 带缓存，所以不要指望运行中编辑 .env 就立即生效，修改后应重启服务。
- 向量维度、切片大小、重叠、模型名和超时等在这里集中配置。

| 常改参数 | 改变的行为 |
| --- | --- |
| `CHUNK_SIZE / CHUNK_OVERLAP` | 新入库文档的切片窗口和重叠 |
| `RETRIEVAL_TOP_K` | 去重后最多保留多少召回片段 |
| `MEMORY_RECENT_MESSAGES` | 最多注入多少条最近消息，不是完整轮数 |
| `DEFAULT_MODEL_PROVIDER` | 请求未指定时的模型提供商 |
| `ALLOW_MODEL_FALLBACK` | 是否允许有限的 DeepSeek → Ollama 降级 |
| `OLLAMA_MODEL` | 本地已安装模型的精确名称 |

改切片参数不会自动重新处理旧文档；改模型或维度也不会自动完成知识库迁移。

### 10.2 API Schema 和 ORM Model 不是一回事

源码：`app/api/schemas.py` 对比 `app/infrastructure/persistence/models.py`。

- Pydantic Schema 负责 HTTP 数据格式、参数校验和响应序列化。
- SQLAlchemy ORM Model 负责数据库表结构及持久化对象。

例如数据库的 `ChatMessage` 使用 `id`、`content`，HTTP 的 `ChatResponse` 则输出 `message_id`、`answer`。代码用 `validation_alias` 从 ORM 属性读入，再以响应字段名返回。

`from_attributes=True` 让响应模型能够读取 ORM 对象属性。文档响应只声明对外可见的字段，因此没有把原始文件路径一起暴露出去。

`ChatRequest` 还会去掉字符串首尾空白、拒绝多余字段、限制问题长度，并校验模型提供商和文档 UUID 列表。

### 10.3 错误如何统一返回

源码：`app/core/errors.py`、`app/main.py`。

业务层可以抛出：

```python
raise AppError("DOCUMENT_NOT_READY", "指定文档尚未就绪", 409)
```

API 异常处理器会统一组织为：

```json
{
  "error": {
    "code": "DOCUMENT_NOT_READY",
    "message": "指定文档尚未就绪",
    "request_id": "本次请求标识"
  }
}
```

请求中间件生成 request_id，响应头也返回 `X-Request-ID`。这里的 request_id 是追踪一次 HTTP 请求用的，不是用户 ID 或会话 ID。

其他主要规则：不存在和越权都返回 404；参数错误返回 422；资源竞争返回 409；上游服务不可用通常返回 503；流式问答使用独立的 SSE 接口。

`BodyLimitMiddleware` 还会在读取请求体时累计大小。文件自身大小限制由上传服务再检查，两层限制针对的对象不同：一个是整个 HTTP body，一个是上传文件。

### 10.4 日志和 LangSmith

`core/logging.py` 输出 JSON 日志。当前 HTTP 日志含请求 ID、用户摘要、耗时、状态码；聊天完成日志含会话 ID、实际 provider、检索数量和总耗时。

不要把“日志支持这些字段”理解成“每条日志都含所有字段”：当前后台入库日志主要用 document ID 关联，也没有完成设计文档中的每阶段耗时分解。

`configure_tracing()` 将配置写入 LangSmith 使用的环境变量，默认隐藏输入/输出；对于未被信任的追踪端点会禁用追踪。它属于可观测性配置，不参与用户身份验证，也不负责知识库检索。

## 11. 记忆和并发：现在做到哪一步

### 11.1 最近消息记忆已实现，自动摘要未实现

当前逻辑会保存完整消息，但运行时只读取最近 `MEMORY_RECENT_MESSAGES` 条，默认 12 条。这里是“条”，不是“12 轮”：一次正常问答通常增加 user 和 assistant 两条。

`summary` 和 `summarized_through` 字段已经存在，工作流也能使用已有摘要；但代码没有自动生成或滚动更新它们的流程。因此，历史对话变长后，老内容并不会自动通过摘要继续进入上下文。

查询会话详情时，`message_limit` 和 `message_offset` 从最近消息向更早的消息分页，选中的这一页再按时间正序返回。

### 11.2 只按单进程开发部署理解当前锁

`SessionService` 按会话 UUID 创建独立锁。它控制单进程内的问答与会话删除竞争，不能当作跨 worker 的分布式锁。

不同会话也可能映射到同一个锁，短暂收到 `RESOURCE_BUSY`；数据库行锁则用于保护相应记录。扩大部署前需要重新设计会话串行与任务执行方式，不是只给 Uvicorn 多加几个 worker 就结束。

## 12. 建议怎样在 IDE 里读和调试

### 第一遍：读最短路径

1. `app/main.py`：看应用入口和 lifespan。
2. `app/application/container.py`：看对象怎样被创建和连接。
3. `app/api/dependencies.py`：看服务和当前用户怎样进入路由。
4. `app/api/routes.py`：找上传和问答两个入口。
5. `app/application/chat.py`：看业务调用顺序与提交位置。
6. `app/workflows/agent.py`：看 Agent、工具预算和引用闭环。

### 第二遍：沿着一个功能进入基础设施

- 跟上传：`IngestionService.upload()` → `process()` → `LocalDocumentLoader` → `QwenEmbedding` → `MilvusVectorStore`。
- 跟问答：`ChatService.answer_v2()` → `AgentRAGWorkflow.run()` → 检索工具 → 模型回答。
- 跟用户：`current_user()` → `Services.user_id()` → `Repository.user()`。

### 建议的断点

| 断点位置 | 观察什么 |
| --- | --- |
| `current_user()` 返回前 | 外部 identifier 如何变成内部 UUID；不要打印密钥 |
| `IngestionService.upload()` 提交前 | 文档对象、内容哈希、服务器生成的路径 |
| `IngestionService.process()` 切片后 | chunk 的正文、ID、页码/章节和数量 |
| `ChatService.answer_v2()` 调用工作流前 | ready_ids、最近消息、summary；确认未混入其他用户资料 |
| `RetrieverService.retrieve()` 召回后 | 结果范围、分数、去重前后数量 |
| `AgentRAGWorkflow.run()` 生成后 | answer、证据登记和引用编号如何对应 |

观察业务内容时使用自己的测试资料；不要把断点里看到的正文、连接串或密钥直接粘贴进公共日志。

## 13. 后续想改功能，应该改哪里

| 想做的事 | 优先入口 | 还需要注意 |
| --- | --- | --- |
| 修改回答风格 | `app/workflows/agent.py` 的 SYSTEM_PROMPT | 不要取消来源约束与不可信数据边界 |
| 调整检索数量/切片大小 | `app/core/config.py` 对应配置 | 旧向量不会自动更新 |
| 新增聊天模型 | `app/infrastructure/llms/providers.py`、`container.py` | 同步更新请求 provider 限制和配置 |
| 调整 MinerU | `app/infrastructure/loaders/mineru.py` 与 `app/application/ingestion.py` | 保留签名 URL 校验、批次续接、缓存签名和短事务 |
| 实现滚动摘要 | 聊天保存流程和会话字段更新 | 原始消息保留；摘要和进度应一致提交 |
| 扩展 V2 SSE | `AgentRAGWorkflow.run()`、V2 流式路由和前端 store | 保持 answer_start 重置、done 权威覆盖与持久化语义 |
| 添加真实认证 | `app/api/dependencies.py` 的 current_user | 保留各层用户过滤，不把鉴权当作过滤的替代品 |
| 升级为按需检索 Agent | 替换/扩展工作流，复用 RetrieverService | 仍需保证检索工具强制用户作用域 |

## 14. 容易误解的地方与当前边界

| 容易误解的说法 | 当前代码实际情况 |
| --- | --- |
| “API 有用户 ID，所以已经安全登录了” | 只是开发期逻辑隔离，请求头可伪造 |
| “保存了聊天记录，所以模型能记住所有历史” | 运行时只取最近消息，自动摘要未实现 |
| “上传返回 202，就能立即提问” | 需要等待状态 ready |
| “所有模型输出都会直接流给前端” | 只有结构化答案中的 answer 增量会发送；工具调用和内部 JSON 不会暴露 |
| “有 MinerU Token 就会使用 MinerU” | 还必须设置 `MINERU_ENABLED=true`；PDF/Office 才会进入云解析 |
| “健康接口返回成功就表示全部服务正常” | live 只说明进程存活；ready 才检查存储和必要配置 |
| “ready 正常说明模型 Key 和答案质量都验证过了” | 模型配置检查只看必要项是否存在，不进行真实生成 |
| “模型返回了引用，所以答案必然忠实” | 代码检查编号，不进行完整事实核验 |
| “依赖创建一次，所有数据库操作共用一个 Session” | 共享的是服务/工厂，具体操作创建自己的数据库 Session |
| “初始化命令就是自动迁移工具” | create_all 创建缺失表，不自动迁移已有表结构 |

当前 PostgreSQL 默认要求 TLS。若配置禁用了 TLS，访问依赖数据库的业务接口可能在 `current_user()` 阶段就失败，不是 Header 语法出错。排查时先看统一错误码，再沿调用链定位。

代码导读不是重新执行服务验收：真实服务是否可达、密钥是否有效，需要在你的环境里按 `README.md` 的步骤联调。本文不包含也不读取真实凭据。

---

## 附：对照资料

- 项目设计目标：`2026-09-09-rag-backend-design.md`。
- 安装、启动与接口示例：`README.md`。

## Agent、网络搜索与 MinerU

一次问答从 `app/api/routes.py` 进入 `ChatService.answer_v2()`：先用短事务保存用户消息，再在事务外运行 `AgentRAGWorkflow`，最后重新检查知识库引用范围并保存完整 assistant 消息。每次请求单独创建 EvidenceRegistry，知识库和网页来源按实际登记顺序共享引用编号。

`knowledge` 只提供知识库工具，`web` 只提供 Tavily，`auto` 根据配置提供两者。网络工具始终使用请求开始时冻结的公开查询，不接收模型临时生成的查询，因此历史消息和私有知识片段不会被拼入 Tavily 请求。

当 `ALLOW_MODEL_FALLBACK=true` 且 DeepSeek 出现连接、超时、429 或 5xx 时，`ModelRuntime` 通过 LangChain middleware 只重试失败的模型步骤。EvidenceRegistry、原生工具消息与已完成工具结果保持不变；切换后本轮余下模型步骤固定使用 Ollama。普通参数错误、输出修复错误和引用错误不会触发降级。

新 PDF、Word、PowerPoint 和 Excel 在 `MINERU_ENABLED=true` 时先验证 PDF/OLE/OOXML 容器，再由 `MinerUParser` 申请签名上传地址、上传、轮询并读取结构化结果；TXT/Markdown 继续由本地 Loader 处理。`documents.parser_metadata` 保存解析阶段和 `batch_id`，超时 retry 会续接远端批次，已有 `normalized.json` 时直接恢复 Embedding。`GET /api/v2/documents/{id}/processing` 只返回安全的状态摘要。

数据库使用当前 ORM 定义显式初始化：`.venv/Scripts/python.exe -m app.bootstrap --postgres`。
- 实际配置字段：`app/core/config.py`；无密钥模板：`../.env`。
- FastAPI 依赖声明与 Annotated 官方说明：`https://fastapi.tiangolo.com/tutorial/dependencies/`。
- FastAPI Header 参数官方说明：`https://fastapi.tiangolo.com/tutorial/header-params/`。

建议先弄清一句话：**dependencies 把“谁在调用”和“可用服务”交给路由，application 管业务，workflow 管问答步骤，infrastructure 才负责与外部系统通信。**
