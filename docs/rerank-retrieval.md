# 第四阶段：复用检索链路加入可选精排

本阶段新增一个约百行的 HTTP 适配器，继续复用现有 RetrieverService、Milvus 混合召回、父块读取、EvidenceRegistry 和消息持久化。没有增加框架、依赖、数据库字段或重建索引。

## 调用链和职责

`Supervisor → RAG Agent → search_knowledge → Embedding → Milvus Dense/BM25/RRF → 权限校验与去重 → 可选 Rerank → Top K → 父块上下文 → 证据登记 → Supervisor 汇总`

1. `app/application/retriever.py` 继续召回 `2 × retrieval_top_k` 个候选（默认 12，最多 60）。先检查所有候选的用户和文档归属，再去重；越权结果直接失败，不发送到精排服务，也不降级为成功。
2. `app/infrastructure/rerank.py` 只使用配置中的固定端点、模型、密钥，按百炼 HTTP 的 `input/parameters` 协议发送叶子文本。模型和用户都无法覆盖连接配置。支持 `output.results` 及顶层 `results` 响应；没有 Jina 等其他协议适配。
3. 返回索引必须是输入候选中的唯一整数，相关性分数必须是 0..1 的有限数字，返回数量必须等于所需 Top K。只按索引选择原始 SearchHit；服务返回的文档正文不参与证据构造。少项、重复、越界、非数字及非法 JSON 均整体回退。
4. 按相关性降序保留 Top K，然后复用父块扩展；精排失败也继续使用原始 RRF Top K。父块扩展不能改写精排分数或轨迹。
5. 引用 `score` 保持原始 RRF 含义，`rerank_score` 是本次请求内的精排相关性分数。引用编号、片段 ID、Excel 行号、原始内容保持各自真实对应关系。父块仍不能产生额外引用编号。

## 配置和成本边界

保持 `.env.example` 中 `RERANK_ENABLED=false`，原有排序行为不变。需要启用时在本机 `.env` 配置：

```dotenv
RERANK_ENABLED=true
RERANK_ENDPOINT=https://YOUR_WORKSPACE.cn-beijing.maas.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank
RERANK_MODEL=gte-rerank-v2
RERANK_API_KEY=YOUR_RERANK_KEY
RERANK_TIMEOUT=5
```

端点必须按账号工作空间和地域从服务商控制台复制，上例仅演示结构。启用时校验 HTTPS、禁止 URL 内嵌凭据/查询串/片段、要求模型和专用配置的密钥；不自动复用其他模型的密钥。默认模型为 `gte-rerank-v2`，服务商具体可用性以账号为准。协议依据：[百炼文本排序 HTTP 文档](https://help.aliyun.com/zh/model-studio/text-rerank-api)。

启用后会将查询和获准的私有候选片段发送到配置的服务商；现有真实 `.env` 没有被修改。密钥只在 Authorization 中传递；不跟随重定向、不自动重试，错误中不保存服务返回正文或 HTTP 异常详情。连接端点属于运维信任配置，只应填写可信服务。

每次知识库工具执行最多一次精排请求，仍受请求工具次数和知识库检索次数限制。精排不是 Agent 生成调用，因此单独记录 `attempts`，不计入生成模型调用数。超时取配置值与本轮剩余时间的较小值；同时检查响应读取期间经过的时间。HTTPX 的网络超时按连接/读取阶段生效，不能保证在所有网络栈情况下精确到毫秒取消同步 I/O；工具返回后 Supervisor 会再次检查总 deadline，耗尽时报 AGENT_TIMEOUT。

精排仅提交查询前 200 字符、每片段最多 1000 字符，同时按候选数量将文本字符预算压到 24000 以内（包括重复查询）；响应上限 64 KiB。字符上限不是特定模型 tokenizer 的精确 token 上限，服务商拒绝输入时会记录降级。截断仅影响排序输入，不改变引用证据。没有分数阈值过滤、熔断器或第二层重试。

## 运行记录

`run_metadata.rerank_events` 为本轮每次精排保存：`status`、`model`、`candidate_count`、`elapsed_ms`、`attempts`、`input_truncated`、`error_code` 和可选 `total_tokens`。同时在知识库 `tool_events[].rerank` 保存轨迹；数据复用现有消息元数据持久化。对象随请求创建，不使用共享的“最后一次结果”缓存。

降级代码包括 `RERANK_TIMEOUT`、`RERANK_AUTH_ERROR`、`RERANK_RATE_LIMITED`、`RERANK_UNAVAILABLE`、`RERANK_INVALID_RESPONSE`，进入工具结果及最终 `warnings`。降级仍可回答原召回证据支持的问题。未命中任何叶子时不调用精排，也不伪造一次执行记录。

## 离线验证和实际效果评估

```powershell
.venv/Scripts/python.exe -m pytest tests/test_rerank.py tests/test_parent_context.py -q
```

测试通过 HTTPX MockTransport 和脚本模型验证，不访问收费服务。覆盖候选提升、精排与父块顺序、RRF 分数保留、引用归属、全部候选权限校验、非法响应、响应体限制、网络异常、禁止重定向、总 deadline 传递和 Supervisor 降级记录。

2026-10-15 验证记录：完整离线回归 170 passed、9 skipped、38 subtests passed；随后补充边界用例并运行精排专项，36 passed。跳过的是需要外部测试服务的集成验证。`uv lock --check`、`git diff --check` 及评估脚本 `--help` 通过，未引入新依赖。前端代码未修改。

已有带 `user_id/document_ids/relevant_chunk_ids` 的标注数据和真实 Embedding/Milvus 时，可运行相同链路的基线和精排评估（两次都会访问实际检索服务，第二次还会调用精排）：

```powershell
.venv/Scripts/python.exe -m scripts.evaluate_retrieval your-labelled-cases.json --no-rerank
.venv/Scripts/python.exe -m scripts.evaluate_retrieval your-labelled-cases.json
```

需事先配置并启用精排。两次保持相同数据、权限、模型和 K，比较 Recall@K、Hit Rate@K、MRR；输出另外保留 RRF 分数、精排分数和降级轨迹。父块不增加命中 ID，因此不能人为抬高召回指标。此阶段未调用真实精排、Milvus 或付费模型，未测量线上质量提升；完整答案忠实度和引用支持度评测仍待后续。
