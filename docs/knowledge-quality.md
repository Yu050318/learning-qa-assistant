# 第五阶段：复杂知识问题规划、Grader 与一次补检索

## 复用位置与新增逻辑

保留现有 Supervisor 的意图识别、RAG/SQL/Web 分工。新增 `app/workflows/knowledge.py`，仅在启用配置时替换 RAG 委派内部执行逻辑：

```text
Supervisor → delegate_rag
  → Planner：一个简单查询，或两个独立事实查询，或要求澄清
  → search_knowledge：沿用 Embedding / Milvus / Rerank / 父块 / 证据登记
  → Grader：检查本任务的原始叶子证据
       answer       → 放行支持来源 → Supervisor 汇总
       clarify      → 询问必要条件，不补检索
       no_knowledge → 说明资料不足
       rewrite      → 针对缺失事实补检索一次 → 再判断 → 结束
```

Planner、Grader 都使用现有 LangChain `create_agent` 和同一个 `ModelRuntime`，不新建 Agent 框架，不安装依赖，不另配模型。不向它们开放任何工具；检索由有界控制逻辑调用原来的 `search_knowledge`，继续执行用户/文档范围校验，保持只读事实来源和编号登记。

SQL、Web 的现有执行和模式授权不变：知识任务的改写只进入用户已获准的私有文档检索，不能用于修改冻结的 Tavily 搜索词、SQL 配置或指定网页授权。

## 充分性和引用

非空候选不再自动意味着可以回答。Grader 同时返回相关性、可回答性、歧义、下一步路由、支持的真实来源编号和带引用摘要。只有 `strong + sufficient + 无歧义` 的 answer 可以放行来源。规划和判断采用 Pydantic 的显式结构校验，额外字段、未知来源编号、重复编号、非法摘要引用、与充分性不一致的 answer 均失败。

Grader 只看到本次任务检索的带编号叶子正文，不接收无编号父块。查询含产品、日期、对象、版本等约束时，提示模型必须保留；用户缺条件返回澄清，缺文档返回资料不足，不能猜测事实。模型判断仍可能出错，结构校验不能证明事实蕴涵或保证不遗漏语义约束；本阶段没有声明准确率提升。

EvidenceRegistry 保留候选的原始编号和成本记录，并增加请求内 `excluded_numbers`。新知识候选默认处于待判断状态；只有通过判断的支持编号能解除排除。判断失败、资料不足或澄清时，未获准候选不会进入 Supervisor 的可用来源、可用父块编号或最终修复来源列表，最终引用校验也会拒绝它们。

已通过的历史委派来源不会因为后一次失败被撤销。混合任务中可继续引用成功的 SQL/Web 来源，并说明知识库缺口；没有任何可用事实证据时，必须返回 insufficient 或 clarification，不能用未通过的片段或历史答案作出 grounded 结论。前端的 `done` 仍以 ChatService 校验和保存后的结果为准，生成中的 SSE 内容属于临时输出。

## 预算与故障

在本地 `.env` 显式启用：

```dotenv
KNOWLEDGE_QUALITY_ENABLED=true
AGENT_MAX_KNOWLEDGE_CALLS=3
AGENT_MAX_MODEL_CALLS=16
AGENT_MAX_TOOL_CALLS=12
```

默认关闭，以便保留旧执行路径并对比结果。现有真实 `.env` 未被修改。

- 每次委派最多两个初始查询；整个用户请求最多一次改写，多个 RAG 委派共享这一额度。
- 最多三次知识检索的默认额度恰好容纳两个初始查询和一次补检索。已经耗尽检索/工具额度时不再发补检索，记录 `RAG_SEARCH_LIMIT_REACHED`；改写已用尽记录 `RAG_REWRITE_LIMIT_REACHED`，返回资料不足。
- Planner、Grader 和它们的模型降级均计入现有全局模型次数、上下文预算、deadline；预算耗尽仍抛原有错误，不增加重试或另开循环。
- Milvus 等工具故障记录 failed，不能解释成正常的“没有资料”。格式错误分别记录 `RAG_PLAN_INVALID`、`RAG_GRADE_INVALID`；模型故障复用现有降级规则，不重新规划或重复已完成检索。
- 空结果不额外调用 Grader，也不自动生成假设答案去检索。本阶段采用缺失事实查询，不实现 HyDE、无限重写或递归子 Agent。

## 执行记录与演示

继续使用既有 RAG Agent 的开始、完成、失败事件，最终答案流保持原接口。`run_metadata` 新增 `knowledge_quality_enabled`、`knowledge_events`、`knowledge_rewrites`、`excluded_source_numbers`，通过现有 ChatService JSON 元数据保存，不增加数据库字段。每个 knowledge event 保存规划查询、各轮判断、补检索查询、状态、错误码、耗时，不记录内部推理文本。

建议演示：

1. “NX-1 的额定功率是多少？”——一个查询，判断后回答。
2. “分别给出 NX-1 的额定功率，以及 2024 年购买产品的保修政策。”——两项事实分别检索，统一编号。
3. “NX-1 的功率和保修政策？”——若功率命中、保修缺失，仅补检索保修；仍不足时说明缺口。
4. “这个产品保修多久？”——产品或适用购买日期不明确时先澄清。
5. `auto`：“统计销量，再结合产品手册分析。”——SQL 与通过判断的知识来源共同汇总；任何部分失败都需说明。

以上是演示问题，不代表真实库中已存在这些产品或条款。

## 验证

```powershell
.venv/Scripts/python.exe -m pytest tests/test_knowledge_quality.py -q
.venv/Scripts/python.exe -m pytest -q
```

专项测试用脚本模型和替身检索，不访问收费服务，覆盖简单/复合规划、一次改写、跨委派预算、歧义澄清、无命中、引用伪造、非法结构、混合 SQL 来源、Grader 故障、模型降级、调用预算及请求状态隔离。真实在线模型、Milvus 与答案质量 A/B 仍未验证，不以离线脚本通过冒充线上效果。

2026-10-15 验证记录：完整后端回归 **204 passed、9 skipped、38 subtests passed**；随后补充跨用户拒绝、deadline 中止、已审核来源保留和 SQLite 历史消息保存用例，知识库与 Excel 联动专项 **66 passed**。`uv lock --check`、`git diff --check` 通过。跳过的是依赖外部隔离服务的集成测试；未修改前端、未调用收费接口、未更改真实数据库或真实 `.env`。

后续已在[第六阶段](answer-evaluation.md)加入答案忠实度/引用支持度评估工具，真实语料质量仍待测量；文档版本、可靠 Worker、Run/Event 断线恢复、共享权限、模型中心仍是后续独立功能。
