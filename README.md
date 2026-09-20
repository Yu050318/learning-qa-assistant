# Learning Q&A Assistant

一个面向多用户的学习问答助手：同时保留 V1 固定检索 RAG，并实现 V2 单 Agent，按请求在用户私有知识库与 Tavily 网络搜索之间选择工具。

代码学习请看 `docs/code-guide.md`：包含分层架构、依赖注入详解、上传/问答调用链和后续修改入口。

V2 架构与安全边界见 [RAG Agent V2 详细架构设计](docs/rag-agent-v2-architecture.md)。

V3 实现依据见 [Vue 前端架构与后端接入方案](docs/rag-v3-frontend-architecture.md)：包含界面、组件与状态划分、文件夹上传、用量展示和后端契约。

## 1. 当前实现

- FastAPI 文档与会话 API、用户作用域查询、统一错误与 request_id。
- 本地 TXT/Markdown、文本型 PDF、DOCX 解析；旧 DOC 通过 LibreOffice 转换。
- 可配置 token 切片、确定性 chunk ID、千问批量 Embedding、Milvus COSINE 检索。
- PostgreSQL 元数据和消息持久化，文档状态机、重试与删除补偿。
- 最近消息上下文、追问改写、服务端引用、DeepSeek/Ollama 回答与 V2 SSE 运行事件。
- DeepSeek 可重试错误发生时，可显式开启一次 Ollama 降级；鉴权错误不降级。
- LangSmith 对 LangChain 模型调用的追踪配置；默认隐藏输入/输出。
- V2 `knowledge / web / auto` 三种模式、请求内证据登记、混合引用和有限工具循环。
- Tavily 固定公开搜索词；网络结果不会写入 Milvus。
- PDF、Word、PowerPoint、Excel 显式启用后统一经 MinerU 云解析；TXT/Markdown 保持本地解析。
- V1/V2 会话版本隔离、V2 运行元数据和显式可重复数据库迁移。

**仍未实现：** 未校验答案的逐 token 输出、自动滚动摘要、正式认证、持久任务队列、多 worker、重排序和网页自动入库。

## 2. 项目结构

```text
app/
  api/                        HTTP 路由、请求/响应模型、依赖与请求体限制
  application/
    container.py              服务装配与就绪检查
    ingestion.py              文档入库、失败重试、删除
    sessions.py               会话 CRUD 与单进程并发控制
    chat.py                   消息保存与问答用例
    evidence.py               V2 来源编号、去重和引用校验
    retriever.py              V1/V2 共用知识库检索
  domain/contracts.py         Chunk、SearchHit、模型/向量库协议
  infrastructure/
    persistence/              SQLAlchemy 模型、仓储与 PostgreSQL 连接
    vectorstores/milvus.py     Milvus schema、用户过滤、向量读写
    embeddings/qwen.py        千问兼容接口
    llms/providers.py         LangChain 模型适配、选择与有限降级
    loaders/local.py          TXT/MD 本地解析与共享切片
    loaders/mineru.py         MinerU 签名上传、轮询、下载和结构归一化
    search/tavily.py          Tavily 搜索适配与结果安全过滤
  workflows/rag.py            V1 固定 RAG
  workflows/agent.py          V2 LangChain Agent 与工具预算
  core/                       环境配置、异常与结构化日志
  bootstrap.py                显式初始化存储，不删除现有数据
  main.py                     FastAPI 入口
```

根目录 `main.py` 和 `scau/agentdemo.py` 保留为原始实验脚本。**新服务从 `app.main:app` 启动**，不会执行实验脚本的 Milvus 初始化。

`frontend/` 是 Vue 3 + TypeScript 前端。开发时先启动 FastAPI，再执行 `cd frontend`、`npm install`、`npm run dev`；Vite 会把 `/api` 和 `/health` 代理到 `127.0.0.1:8000`。生产构建使用 `npm run build`。

## 3. 安装和配置（Windows PowerShell）

工作目录为本项目根目录，Python 3.12+。依赖已经声明在 `pyproject.toml` 并锁定在 `uv.lock`。

```powershell
# 如果没有 uv，先在虚拟环境之外安装它
python -m pip install uv
uv sync --locked
```

保留现有 `.env`，只参考 `.env` 补充缺少的项，不要覆盖已有密钥。配置文件按项目根目录定位，不依赖当前终端所在位置。环境变量优先于 `.env`；`DATABASE_URL` 优先于 `DB_URL`。

### PostgreSQL

云端连接默认要求 TLS：URL 使用 `sslmode=require`，正式部署推荐配置证书并使用 `verify-full`。当前如果 URL 中是 `sslmode=disable`，业务接口和 readiness 会返回 `DATABASE_TLS_REQUIRED`，但 `/docs` 和存活接口仍可打开。不会偷偷覆盖 URL。

只有在确认开发环境受信任、并明确接受明文连接风险时，才在当前终端显式临时关闭此限制：

```powershell
$env:DATABASE_REQUIRE_TLS = 'false'
```

这只取消应用的 TLS 校验，实际连接方式仍由原 URL 的 sslmode 控制；不要把这个设置用于公网部署。

### Milvus 和 Embedding

默认连接本机 `http://localhost:19530`，数据库 `rag_tutorial`，新集合 `rag_docs_text_v4_1024`。请先启动可访问的 Milvus，或配置 `MILVUS_URI`、`MILVUS_TOKEN`。本项目不自动安装或启动 Milvus。

默认选择文本模型 `text-embedding-v4`、1024 维、每批最多 10 条，不沿用实验脚本中的 `qwen3-vl-embedding`。默认 DashScope 地址是北京地域的文本 Embedding 兼容接口；密钥所属地域不同需要同时调整 `DASHSCOPE_BASE_URL`。多模态接口不是本轮适配范围。

集合描述记录模型和维度签名，读写时校验，避免相同维度但不同模型混用。切换模型或维度必须换新集合并重新入库；旧文档迁移工具暂未实现。请在第一次上传前确认模型配置。

### Ollama

默认地址 `http://localhost:11434`；`OLLAMA_MODEL` 必须填写已安装模型的精确名称，可通过 `ollama list` 查看。默认 `qwen3.5:9b` 不会自动下载。设置 `ALLOW_MODEL_FALLBACK=true` 后，DeepSeek 遇到连接、超时、429 或 5xx 时只重试当前模型步骤并切换一次 Ollama；已完成的检索和 Tavily 请求不会重跑。

### LangSmith

官方 US 默认端点为 `https://api.smith.langchain.com`，注意 `.com` 不能写成 `.co`。为避免向拼错的域名发送 API Key，非预设官方 HTTPS 域名会禁用追踪，并记录 `langsmith_disabled_untrusted_endpoint`；不影响问答本身。只有确认是自己的 HTTPS 私有部署，才设置 `LANGSMITH_ALLOW_CUSTOM_ENDPOINT=true`。

默认 `LANGSMITH_HIDE_INPUTS=true`、`LANGSMITH_HIDE_OUTPUTS=true`，不上传完整问题/检索正文/回答；追踪仍可能包含模型名和调用元数据。不开启完整正文追踪，除非确认资料可发送到该平台。

### MinerU 与 Office/PDF

设置 `MINERU_ENABLED=true` 并配置 `MINERU_API_TOKEN` 后，新 PDF、DOC/DOCX、PPT/PPTX、XLS/XLSX 会上传到 MinerU 云端解析；未显式启用时这些格式在创建数据库记录前返回 503。上传前会校验 PDF/OLE/OOXML 容器，解析超时后可沿用已有 `batch_id`，Embedding 重试复用本地规范化缓存；进程重启会恢复 processing 文档，若上次停在上传阶段则创建新的安全批次。TXT/MD 使用 UTF-8 且不会上传到 MinerU。`INGESTION_WORKERS` 默认 2，用于限制本进程同时处理的文档数。

### Tavily

设置 `WEB_SEARCH_ENABLED=true` 并配置 `TAVILY_API_KEY` 后，V2 的 `web` 和 `auto` 模式可调用网络搜索。联网时只发送当前问题或显式 `web_query`，不会发送历史消息或知识库片段。应用会阻止明显的密钥、连接串和超长公开查询，但这不是完整的隐私识别；发送前仍需确认查询可公开。

## 4. 初始化与启动

应用启动**不会**自动创建云端表或 Milvus 集合。确认数据库和向量库配置后，显式执行：

```powershell
.venv/Scripts/python.exe -m app.bootstrap --postgres
.venv/Scripts/python.exe -m app.bootstrap --migrate-v2
.venv/Scripts/python.exe -m app.bootstrap --milvus
.venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

PostgreSQL 初始化与 V2 迁移都只操作 `rag_v1` schema；迁移新增字段、约束和版本记录，重复执行不会清空数据。

- 交互式接口文档：`http://127.0.0.1:8000/docs`
- 存活：`GET /health/live`，不连接外部服务。
- 就绪：`GET /health/ready`，检查表、Milvus 集合兼容性和必要模型配置，不调用收费模型。

当前仅支持单进程开发部署，不要启用多个 Web worker。后台入库有持久状态和启动恢复，但执行器仍在应用进程内；需要横向扩容时再增加数据库任务租约。

## 5. 最小联调

业务接口均要求 `X-User-ID`。这只是可伪造的开发标识，**不是认证**，禁止直接暴露到不可信网络。两个用户的相同文件允许分别上传；同一用户重复内容返回 409。

```powershell
$headers = @{ 'X-User-ID' = 'alice' }

# 上传已有本地文件；Windows 请使用 curl.exe 而非 PowerShell 的 curl 别名
curl.exe -X POST http://127.0.0.1:8000/api/v1/documents `
  -H 'X-User-ID: alice' -F 'file=@knowledge.txt'

# 保存上传响应中的文档 ID，轮询到 ready 后再问答
$documentId = '替换为上传返回的文档UUID'
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/documents/$documentId" -Headers $headers

$session = Invoke-RestMethod http://127.0.0.1:8000/api/v1/sessions `
  -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes('{"title":"学习助手"}'))

$body = @{ question = '这份资料主要讲什么？'; document_ids = @($documentId); model_provider = 'deepseek' } | ConvertTo-Json
Invoke-RestMethod "http://127.0.0.1:8000/api/v1/sessions/$($session.id)/messages" `
  -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' `
  -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
```

未传 `document_ids` 时使用当前用户全部 ready、且模型匹配的文档；传入空列表会被拒绝，避免误当作全库查询。检索结果为空时，返回资料不足提示，`model_provider=none`、`model_name=retrieval-only` 表示没有调用答案生成模型。

## 6. API 与错误

| 方法 | 路径（除健康接口外都加 /api/v1） | 行为 |
| --- | --- | --- |
| POST | /documents | 上传，202；后台入库 |
| GET | /documents | 当前用户文档，offset/limit 分页 |
| GET | /documents/{id} | 状态、切片数、脱敏错误 |
| POST | /documents/{id}/retry | 重试 failed 或中断的 processing，202 |
| DELETE | /documents/{id} | 删除向量、原文件和记录，204 |
| POST | /sessions | 创建会话，201 |
| GET | /sessions | 当前用户会话，offset/limit 分页 |
| GET | /sessions/{id} | 会话与最近消息，message_limit/message_offset 分页 |
| DELETE | /sessions/{id} | 硬删除会话，数据库级联删除消息，204 |
| POST | /sessions/{id}/messages | 非流式问答与引用 |
| POST | /sessions/{id}/messages/stream | 本轮明确返回 501，不写消息 |

V2 会话使用独立路径；文档上传仍复用 `/api/v1/documents`：

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| POST/GET | `/api/v2/sessions` | 创建或列出 V2 会话 |
| GET/DELETE | `/api/v2/sessions/{id}` | 读取或删除 V2 会话 |
| POST | `/api/v2/sessions/{id}/messages` | Agent 非流式问答 |
| POST | `/api/v2/sessions/{id}/messages/stream` | SSE 输出运行状态、用量与已校验的最终回答 |
| GET | `/api/v2/documents/{id}/processing` | 查看解析阶段，不暴露上游任务 ID |

V2 请求示例：

```json
{
  "question": "结合我的资料和最新公开信息回答",
  "search_mode": "auto",
  "document_ids": ["文档UUID"],
  "web_query": "可选的公开搜索词",
  "model_provider": "deepseek"
}
```

`knowledge` 禁止 `web_query`；`web` 禁止 `document_ids`；`auto` 可以同时授权两种来源。网页引用与知识库引用使用同一连续编号。

资源不存在与越权访问均返回 404。忙碌资源返回 409。响应不暴露原文件路径、凭据或上游原始错误：

```json
{"error":{"code":"DOCUMENT_NOT_READY","message":"指定文档尚未就绪","request_id":"..."}}
```

## 7. 一致性与当前限制

- 入库前提交 processing；锁定文档行，清理旧向量，再分批 Embedding/upsert；全部成功才 ready。失败清理是尽力执行，清理失败时由下次重试继续，failed 文档不会被检索。
- 进程退出可能留下 processing；确认原任务已停止后调用 retry。正在运行的任务持有数据库行锁，重试/删除返回 409。重复后台任务遇到 ready 会跳过。
- 删除先提交 deleting，向量或文件清理失败时保留记录，重复 DELETE 可继续。完全删除后再次 DELETE 返回 404，不承诺重复请求仍为 204。
- 会话问题先落库；只有完整成功才保存 assistant。生成失败时仅保留用户问题。消息原文一直保留至会话被删除；运行时只读取最近 N 条。
- summary/summarized_through 字段已存在并可被读取，但本轮不自动生成或推进摘要。token_usage 是最终答案调用的用量，不包含查询改写等整次请求总用量。
- 引用只来自本次召回且在答案中被引用的编号；未知编号直接报错。该校验不等价于回答事实正确，忠实度评测仍需后续补充。
- 检索目前仅去除完全重复正文，没有语义重排或近似重叠去重。
- 会话使用固定 64 个分片锁控制单进程并发，不同会话可能短暂竞争同一锁。多 worker 需要改为数据库级会话串行机制。
- 本地解析具备文件/解压大小、页数、文本量、切片数量限制；DOC 有子进程超时，但 PDF/DOCX 解析尚未放进隔离进程。对外服务前须补解析沙箱、速率限制、正式认证和入口限制。
- 当前没有迁移旧集合、孤儿文件自动回收和多 worker 持久任务协调；单 worker 可通过本地规范化缓存和已有 MinerU `batch_id` 显式 retry 恢复。

## 8. 本轮验证与密钥安全

离线测试使用 SQLite、脚本化 LangChain 模型和 HTTP 替身，覆盖 V2 路由、会话版本、引用、Tavily 边界、MinerU 四类归一化、批次恢复、模型单步降级和 Agent 输出修复。`tests/fixtures/rag_v2_cases.json` 保存 30 题固定质量集。

2026-09-20 已实测 PostgreSQL 行锁、V1/V2 隔离、Milvus 用户过滤，以及真实 Qwen Embedding、DeepSeek knowledge 问答和 Tavily web 问答。MinerU 结果下载会重试临时网络故障；Windows 上 Python/OpenSSL 与结果 CDN 握手不兼容时，会使用系统 `curl.exe` 的 Schannel 安全下载，仍然校验证书并限制结果大小。

基础检查：

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_*.py' -v
$env:RUN_STORAGE_INTEGRATION='1'
.venv/Scripts/python.exe -m unittest tests.integration.test_storage_v2 -v
.venv/Scripts/python.exe -m compileall -q app tests
.venv/Scripts/python.exe -m pip check
```

`.gitignore` 已排除 `.env`、本地运行日志和上传目录。复制 `.env.example` 为 `.env` 后再填写本机凭据；不要提交真实密钥。已经公开或粘贴到不可信位置的凭据建议轮换。

## 9. 后续范围

后续按需增加逐 token 暂定回答与断线恢复、事务式滚动摘要、正式认证、可靠任务队列、多 worker 协调、重排和网页自动入库。

## 10. 检索召回率评估

先准备 JSON 评估集。每道题必须人工标注相关切片；没有相关切片标注时，无法计算召回率：

```json
[
  {
    "id": "q01",
    "user_id": "用户 UUID",
    "question": "服务器维护负责人是谁？",
    "document_ids": ["文档 UUID"],
    "relevant_chunk_ids": ["正确答案所在切片 UUID"]
  }
]
```

运行真实的 Embedding + Milvus 检索链路：

```powershell
.venv/Scripts/python.exe -m scripts.evaluate_retrieval retrieval-eval.json
```

输出包括宏平均 `recall@1/3/6`、`hit_rate@1/3/6` 和 `mrr`。评估集应覆盖真实问题、同义改写、跨段问题和无答案问题；无答案问题应另算误召回率，不要混入 Recall 的分母。
