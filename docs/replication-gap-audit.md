# SuperMew 功能复现差异核查

核查日期：2026-10-17。参考代码：`D:/develop/项目/企业级rag平台/SuperMew`，HEAD `eb0495e`；当前 Demo：`D:/Python/LangchainDemo`，HEAD `4a95e3d`，另有本轮新增 Docker 部署文件。本报告比较这两个本地快照，不代表远端上游最新版，也不以原项目 README 的宣称替代代码检查。

结论：当前 Demo 完成了自己的 13 阶段实现和隔离验收，但没有达到参考项目全功能等价。缺口同时包括未实现的平台功能、已有能力的简化实现，以及磁盘配置中尚未启用的功能。不能用一个开关显示所有缺失功能，也不应把通过现有测试理解为复现完整。

## 1. 核查边界与历史范围

- 回看了原复现聊天的首条请求、最初功能对照表、用户确认的 Supervisor 首期计划及后续交付记录。
- 首期明确选择“复用现有 Demo，不复制参考项目整套运行平台”；SQL 通过服务端配置，不新增管理页面。最初建议暂缓 Skill/Sandbox 等外围模块。因此这些属于相对参考项目的产品差异，不能全部认定为首期违约。
- 但后续最初对照表明确列过取消、HITL、按角色配置模型、完整评测与管理页面；最终 13 阶段并没有覆盖这些能力的全部语义，完成范围需要纠正。
- `SuperMew_Codex_Implementation_Plan.md` 是参考仓库的二次开发计划，包含拟新增设计，不直接当作 Demo 已承诺或原项目已实现的证据。以实际路由、服务、模型和前端调用为准。
- 本次是代码和配置差异核查，没有修改业务实现、启用开关、升级数据库、重新执行收费模型调用或重新跑验收。现有隔离验收结果仅引用 `docs/validation/final-acceptance.md`，不冒充本次实测。
- 功能关闭结论来自本地 `Settings()` 读取当前环境与磁盘 `.env`；没有读取运行中后端的环境快照，没有判断附加数据库表是否已初始化。

## 2. 相对参考项目尚未完整实现的功能

| 编号 | 功能 | 参考项目代码证据 | Demo 当前情况及用户影响 |
| --- | --- | --- | --- |
| G01 | 平台管理员角色与对应管理导航 | `backend/api/routes/auth.py` 的角色注册/登录响应；`frontend/src/components/Sidebar.vue` 的 `isAdmin` 导航 | `User` 没有平台 role，登录只返回 id/username；模型管理使用服务端用户名白名单，组织另有角色。没有原项目相同的管理员账号体验。参考项目也不是单独 `/admin` 网站，而是登录后按角色展示管理入口。 |
| G02 | Skill/Tool 持久注册、版本与能力管理后台 | `backend/api/routes/capabilities.py`；`backend/capabilities/`、`backend/skills/`、`backend/tools/`；`CapabilityAdmin.vue` | 当前 `/api/v2/capabilities` 只返回模型、上传及功能开关，不能创建/修改 Skill 或 Tool，也没有通用能力注册控制面。固定 Supervisor 委派工具不等价于可管理的注册系统。 |
| G03 | 能力中心与命令面板 | 参考 `App.vue` 装载 `CapabilityCenter`、`CommandPalette`，注册 Ctrl/Cmd+K | 当前只有 knowledge/sql/web/auto 模式及文件/共享库选择，没有能力目录和命令面板。 |
| G04 | HITL 暂停、人工补充及恢复同一 Run | `backend/api/routes/runs.py` 的 `/resume`；`backend/rag/checkpoint_runner.py`；`ApprovalDialog.vue` | 当前能返回 clarification，但用户补充会发起新问答，没有 waiting/checkpoint、一次性恢复标识与同一运行恢复。最初对照表列过，尚未补齐。 |
| G05 | 正式取消问答运行 | 参考 runs 路由 `/cancel`、取消状态及 cancellation registry | 当前 Run 状态只有 running/succeeded/failed/interrupted，没有取消 API 或前端停止执行按钮。断开浏览器流不等于取消后台模型/工具执行。 |
| G06 | 持久评测平台与评测管理页面 | `backend/api/routes/evaluations.py`、`backend/evaluation/`、`backend/workers/evaluation.py`、`RagEvaluationWorkbench.vue` | 当前有答案/检索评估 CLI 和评分逻辑，但没有 Dataset/Job/Case 管理 API、独立评测 Worker 和管理工作台；不能在页面导入数据集、运行/取消评测任务并检查逐题结果。 |
| G07 | 固定企业语料与真实基线对比闭环 | 参考 `scripts/generate_synthetic_enterprise.py`、`evals/rag/synthetic_enterprise/`、`docs/second-dev/s4e-quality-analysis.md` | Demo 只有题目提纲、十题合成评分器样例及功能联调样例，未发现对应的确定性 12 文档/80 题生成资产和真实 A/B 质量报告。不能把故意注错的评分器样例当作问答质量或提升指标。 |
| G08 | Step-back / HyDE 查询重写策略 | 参考 `backend/rag/pipeline.py` 的 `rewrite_method`、`step_back_question`、`hyde_document` | 当前 `workflows/knowledge.py` 有问题拆分、Grader 和至多一次缺失事实改写补检索，但没有这两种具名策略及选择/校验链路。它是功能简化，不是完全没有查询改写。 |
| G09 | 通用工具审批、隔离执行与产物展示链路 | `backend/guardrails/approvals.py`、`backend/sandbox/docker.py`、前端 `Artifacts/` 和审批组件 | 当前有 SQL 只读校验、URL/SSRF 防护、来源校验和预算，但没有通用 Run-bound 审批、隔离执行 Sandbox 和相应产物链路。不能称为“没有安全措施”；缺的是参考项目的通用平台能力。 |

G01/G02/G06 是管理员界面差异的主要来源。启用模型中心只能解决已有模型页面的可见性，不能补齐这些管理功能。

## 3. 已有实现但与参考项目不等价

| 编号 | 功能 | 当前已有 | 仍缺少或有差异 |
| --- | --- | --- | --- |
| P01 | 模型中心 | 管理员白名单、配置修订、防并发覆盖、运行内配置快照、目录检测、调用/全局用量统计 | 参考 `backend/model_control/contracts.py` 的 Answer/Fast/Grader/Evaluator 分配与可增删 Profile 没有复现。当前 `ManagedModels` 固定 DeepSeek/Ollama 两个槽位，地址和密钥仅在服务端；有内存快照及修订记录，但不能等同于完整持久模型角色快照/控制面。 |
| P02 | 可恢复 Run/Event | 持久事件、请求幂等、游标重放、刷新恢复展示、助手消息与终态原子提交 | 当前执行仍在 API 的线程池中；过期进入 interrupted，不从模型/工具检查点接管。未复现参考 `backend/runs/` 的完整 owner lease/fencing、跨实例取消及 `backend/events/` 的 journal/outbox/Redis 通知体系。事件能重放，不代表执行能恢复。 |
| P03 | 文档版本及安全发布 | 独立文档 ID/源哈希、版本列表、成功后显式激活；`activate_document_version` 使用 expected_current 和行锁，已有 CAS 含义 | 没有完整 Candidate Build、索引 Manifest 写后核验、BuildProfile 冻结和引用/租约感知的 retirement/cleanup 工作流。当前已有三级树 manifest/fingerprint，不应误报为“完全没有 manifest”；缺的是原版完整索引发布协议。参考 `backend/documents/publication.py`。 |
| P04 | 独立入库 Worker 与任务管理 | 有 PostgreSQL 持久任务、租约心跳、有限重试、独立 `app.worker`；前端有文档处理状态 | 没有原版统一上传/删除 Job 列表与管理体验；删除仍主要沿用同步 deleting/补偿链，未复现独立的持久 Retirement Job 全流程。当前 Docker Compose 还固定 in_process 且没有 Worker 服务。 |
| P05 | 多知识库与租户权限语义 | 有组织、成员、viewer/editor 授权、共享库创建/选择、撤权复核、按文档归属隔离 | 是基于个人文档归属的组织共享方案，不等价于原版 Tenant/KB/DocumentVersion/Index 的完整运行与冻结检索 scope。需保留现有 ACL，不能简单用统一管理员权限替代用户所有权。 |
| P06 | RAG 追踪与证据 UI | 真实叶子引用、Excel 行号、版本信息、SQL/Web 来源卡、Agent 起止状态、部分 metrics | 没有原版完整 RAG 步骤/Trace、KnowledgeContextPanel、Run timeline、审批与产物等组件组合。原页面视觉、主题、导航也没有逐屏复刻；当前是另做的 Learning Q&A 界面。 |
| P07 | 入口安全与运行治理 | JWT/刷新轮换、Cookie Origin 校验、请求大小限制、SQL/URL 边界、每轮预算及 Run 槽位限制 | 未发现原版登录身份/IP 等 HTTP 限流、中间件安全响应头体系及完整治理配置。请求内预算和 429 容量拒绝不等价于入口身份限流。参考 `backend/rate_limits/`、`backend/security/headers.py`。 |
| P08 | 数据库迁移、跨端契约与质量门禁 | 显式 bootstrap、Pydantic schema、手写 TS 类型、单元/集成测试、历史浏览器验收记录 | 未复现 Alembic 迁移链、JSON Schema 真源/生成类型、Registry 校验和同等范围的自动化 E2E/故障注入门禁。差异不意味着既有测试无效，而是现有通过范围小于原平台能力范围。 |

现有版本切换、权限隔离和持久事件均有真实实现，不能为了补齐命名而另写平行流程。后续补齐应沿现有服务扩展。

## 4. 已实现但当前未启用的能力

以下是核查时的本地有效 Settings，不输出任何密钥或账号名。附加表是否存在尚未核查；不能仅改开关就认定可用。

| 能力 | 当前设置 | 启用前准备 |
| --- | --- | --- |
| 模型中心页面 | `MODEL_CENTER_ENABLED=false`，管理员名单 0 项 | `bootstrap --model-center`；确认已有账号归属并配置白名单；重启验证管理员/普通用户权限 |
| 组织与共享知识库 | `SHARING_ENABLED=false` | `bootstrap --sharing`；重启后验证组织成员与授权 |
| 文档版本管理 | `DOCUMENT_VERSIONS_ENABLED=false` | `bootstrap --document-versions`；重启后验证版本与激活 |
| 独立持久入库 Worker | `INGESTION_MODE=in_process` | `bootstrap --ingestion-jobs`；API/Worker 同设 durable、共享目录；启动 `python -m app.worker`；若 Docker 部署，需同步修改 Compose |
| Run/Event 持久化与重放 | `CHAT_RUNS_ENABLED=false` | `bootstrap --chat-runs`；重启后验证游标与断线；不会获得 HITL/取消能力 |
| 三级分块与自动合并 | `HIERARCHY_ENABLED=false` | `bootstrap --hierarchy`；旧文档按阶段说明显式补建；当前两层父级上下文仍开启 |
| 知识规划与 Grader | `KNOWLEDGE_QUALITY_ENABLED=false` | 无新增表；确认模型预算后启用并验证 |
| Rerank | `RERANK_ENABLED=false` | 配置合法 endpoint/model/key 后启用；会增加上游调用 |

核查时联网搜索与 MinerU 配置为开启；此处只说明开关，未验证本轮真实 API 连通或收费服务质量。当前 `.env.example` 默认关闭高级特性，直接按模板部署同样不会自动显示完整能力。

## 5. 已复现的主链路，不应重复开发

- 登录、刷新、退出及用户私有资料/会话隔离。
- Supervisor + RAG/SQL/Web 的有预算委派、统一来源、流式答案与历史记录。
- 独立 PostgreSQL 业务源的只读 SQL/白名单，Tavily 搜索和 Scrapling 授权网页读取。
- Dense + BM25 + RRF，XLSX 表头/Sheet/物理行来源；父级上下文、可选精排和三级合并已有代码。
- 文档版本、持久入库 Worker、Run/Event 重放、组织共享、模型中心和评估 CLI 均有实现，差异见上，不能一概标记“没做”。

阶段测试和真实隔离联调覆盖见 `docs/validation/final-acceptance.md`。这些记录没有证明全量原项目功能已复刻，也没有证明当前普通启动环境已开启同样的配置。

## 6. 建议补齐顺序与验收

1. **先解决能看到/能使用的问题**：确认平台管理员角色方案，安全初始化并启用需要的既有功能，补清晰的管理导航。验收必须包含普通用户无管理权限、管理员有正确入口；不能直接把所有账号设为管理员。
2. **补最初清单的明确缺口**：取消 Run、HITL、按用途分配模型、评测工作台/任务/数据集与固定企业基线。验收分别覆盖断线与取消区别、同 Run 恢复、配置冻结、真实可复现评测。
3. **补运行和索引的完整语义**：Candidate/Manifest/发布与清理、检索冻结 scope、跨实例租约/outbox，以及独立 Worker 的实际部署。现有 CAS/权限/幂等应延续，不能退化。
4. **再补通用能力平台与界面一致性**：Skill/Tool 管理、能力中心、审批/Sandbox/产物、完整 Trace 与命令面板。它们在原首期曾明确暂缓，作为独立阶段处理。
5. **上线前补入口治理与迁移/契约门禁**：登录/接口限流、安全头、可升级迁移与覆盖上述新流程的浏览器验收。

不建议在同一批变更中同时更换 Qdrant、重做管理员权限和改动发布协议：这些是独立目标，合在一起难以定位数据与权限回归。本次用户要求是排查，未开始上述功能开发或存储迁移。

## 7. 关键定位

Demo：

- `frontend/src/router.ts`：页面清单；`frontend/src/App.vue`：导航显隐。
- `app/api/routes.py`：普通能力发现、文档、会话、Run 查询/事件入口。
- `app/api/model_center.py`、`app/application/model_center.py`：模型管理实际范围。
- `app/application/runs.py`：当前事件持久化与执行边界。
- `app/application/ingestion.py`、`app/infrastructure/persistence/repository.py`：入库、删除与 CAS 版本激活。
- `app/workflows/knowledge.py`：规划、Grader 与一次补检索。
- `app/application/evaluation.py`、`scripts/evaluate_answers.py`、`scripts/evaluate_retrieval.py`：现有 CLI 评测。
- `app/core/config.py`、`docs/validation/final-acceptance.md`：开关和既有验收边界。

参考项目：

- `frontend/src/App.vue`、`frontend/src/components/Sidebar.vue`：实际页面与管理员导航。
- `backend/api/router.py`、`backend/api/routes/{capabilities,models,evaluations,runs,documents,auth}.py`：平台/API 差异。
- `backend/rag/{pipeline,checkpoint_runner}.py`：策略与恢复。
- `backend/model_control/contracts.py`、`backend/documents/{publication,retrieval}.py`：模型角色、发布协议与冻结 scope。
- `backend/{events,guardrails,sandbox,rate_limits,security}/`：事件与通用治理。
