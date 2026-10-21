# 阶段七：能力注册、执行和入口

最新验收与部署证据见 [阶段七验收](validation/stage7-acceptance.md)。下文“当前状态”的逐轮计数保留实施历史，最终状态以上述报告和进度表为准。

开始条件：阶段六已于 2026-10-20 推送，远端 main 为 `f517c092467724ffbb67bcfc27c077fde24af8ed`。此文为实施及验收清单，不是功能完成证明。

## 核对来源

参考为本地 SuperMew `eb0495e` 的 `backend/capabilities/{control_contracts,control_repository,control_service,catalog}.py`、`backend/skills/registry.py`、`backend/tools/{catalog,registry,custom_http,control}.py`、`backend/api/routes/capabilities.py` 及前端 `components/Capabilities/{CapabilityAdmin,CapabilityCenter,CommandPalette}.vue`。参考实现不仅有目录页面，还包含声明式 HTTP Tool、Skill 指令与工具白名单、版本和运行装配。不能只新增几张静态卡片就宣称对齐。

## 实施范围

1. 沿用现有 PostgreSQL/Alembic 和管理员角色，持久保存 Skill、HTTP Tool、版本历史、变更者及目录修订。内置知识/SQL/Web 能力投影到同一目录，禁止自定义工具覆盖内置名称。创建、更新、启停和删除均做服务端鉴权及并发修订校验；被引用工具不能删除，已启用 Skill 不能依赖停用工具。
2. Skill 保存说明、指令、允许工具、角色/Secret 需求和启用状态；创建初版，更新递增，历史不可覆盖。目录展示可用性及原因，不返回 Secret 值。管理端支持真实增删改、详情、版本、启停；SQL/Web 配置与现有只读白名单和公开搜索限制相连，不能另建绕过原安全边界的执行路径。
3. HTTP Tool 支持 GET/POST、对象参数 schema、声明式头、服务端 Secret 引用、超时、响应字节上限、幂等与审批标记。复用现有 URL/DNS 固定连接能力并补足 JSON 请求/响应处理；仅允许公共 HTTPS，逐次校验跳转/解析目标，不泄漏凭据到其他主机或日志。不执行上传代码或任意 Python。输入 schema 大小、深度、节点和引用有界，并在调用前验证参数；错误以受控结果进入执行记录。
4. 将真实 Skill 选择、指令和工具白名单接入当前 Supervisor，不改变检索 ACL、公开查询脱敏、预算、来源证据或私有记忆边界。普通模式的能力发现与显式 Skill 激活使用同一目录。Run 入队冻结目录及所需版本，恢复使用冻结内容；用户撤权和平台停用仍在执行前复核，旧检查点不得静默补成新配置。工具调用纳入现有计数、取消、超时、检查点和事件，不能绕过 Worker 或恢复后重复有副作用调用。
   自定义公共 HTTP 参数不得由已见过私有 RAG/SQL/历史内容的 Supervisor 自由生成后直接外发。沿用既有冻结公开词边界，由仅能看到本轮公开输入的工具专职执行路径生成参数，结果再回传；需要明确公共输入的请求在缺少输入时拒绝或澄清。真实工具结果必须进入来源证据和预算，不能借新工具绕过引用校验。
5. 能力中心支持 Skill/Tool 搜索、可用性筛选、权限/网络/审批信息和当前对话激活。全局 Ctrl/Cmd+K 提供搜索、上下选择、回车确认、Esc 关闭和焦点恢复；选项依真实目录及角色生成。切换账号/空间不沿用未授权选择，目录加载失败不能展示伪造可用状态。保留现有导航和窄屏可用性。
6. 通用 Run 审批与 Sandbox 属阶段八；本阶段需保存和展示审批要求，依赖尚未交付审批的工具必须明确不可执行，不能默认放行或将其隐藏为已完成。

## 验收门禁

- 单元/API：普通用户无管理权限；字段、角色和 schema 边界；未知/禁用/被引用能力；并发版本冲突；Secret 不回传；恶意 URL、DNS、跳转、响应过大和超时；同 Run 冻结/撤权/取消/恢复及非幂等工具不重复执行。
- 真实 PostgreSQL：空库升级、旧资料保留、默认目录幂等、并发更新和历史一致。独立 Worker 应能立即读取已提交修订，不依赖 API 内存注册。
- 浏览器：管理员创建 Tool/Skill、编辑版本、使用该 Skill 完成真实测试 HTTP 调用，再停用/删除；普通用户只能使用有权限能力。实际验证能力中心、命令面板、空态/错误、键盘和窄屏。
- 完整前后端回归及 Docker API/Worker 部署；业务库先备份并检查新表和开关后启用，保留历史索引/会话及第五阶段报告。验证 HTTP Tool 可使用隔离受控上游，不冒充真实问答质量。
- 完成本阶段全部范围和证据后提交推送，再进入阶段八。任务结束仅删除本次生成且不再使用的 Docker 镜像，不执行全局 prune，不删除现有数据库或数据卷。

## 当前状态

已核对参考的注册/版本/依赖保护、HTTP 运行时、目录及键盘界面，并定位当前 Supervisor 的固定三个委派工具与 Run 冻结入口。

已新增声明/目录校验、持久控制面和管理员 API；`0009_capability_control` 初始化三个内置 Skill，保存目录修订及完整历史，禁止名称覆盖/删除后复用，检查引用依赖和 Secret 名称白名单。JSON Schema 使用锁定的 jsonschema 4.26.0 校验 draft 2020-12，禁止远程引用及外部基准。库用法依据 [官方验证说明](https://python-jsonschema.readthedocs.io/en/stable/validate/)，不自行重写 schema 引擎。

声明/控制面及相关 API/发布门禁回归 **78 passed、8 subtests passed**；独立 PostgreSQL 迁移/并发 **30 passed**，包括旧身份保留、默认目录幂等和双实例修订竞争。原始测试 XML 位于忽略目录 `data/validation/stage7-control*.xml`。

新增 HTTP JSON 工具运行时，复用网页读取的固定 DNS 传输；支持参数验证、Secret 解析、GET/POST、拒绝重定向、响应大小限制和取消探针，域名解析占用有界。不保存含原始凭据回显的响应，非幂等网络失败返回结果未确认。HTTP/原网页读取组合 **33 passed**，测试使用可观察的传输替身，尚未做完整 Worker 内调用或实际外部 API 验收。

Run 已冻结目录、所选 Skill、允许工具与精确历史摘要；更新后的指令/HTTP 端点不会改写旧运行，停用、角色限制和 Secret 名单变化会在执行前阻止调用。最终发布持有目录读锁，避免权限检查后配置并发变化。无 Skill 的旧请求继续保持原幂等指纹；启用能力后缺少冻结版本的旧运行拒绝执行，不自动补成新版本。

已将目录发现和 HTTP 调用接入 Supervisor。参数由仅看到冻结公开输入的专职 Agent 生成，不接收 Supervisor 任务、私有历史、摘要或检索内容；HTTP 结果进入证据预算、真实引用、工具计数和检查点。引用面板显示工具名和版本。恢复测试验证：HTTP 成功后丢失 Worker 所有权，接管复用原回执和引用，不再次发请求；非幂等调用断线后接管保留结果未确认状态，不重发副作用。

执行/控制/已有 Run 组合先有 **45 passed**；随后扩展实际接管专项 **6 passed**。使用真实 LangChain/Supervisor、SQLite 持久回执与可观察 HTTP/模型替身，不冒充实际外部 API 或收费模型质量。前端 **25 项 Node 测试**及构建通过。

评测任务现已冻结创建时目录，各题复用同一版本；启用后拒绝为旧评测自动补目录，能力版本不同的 A/B 任务不比较。新增 `/skill-name 问题` 激活和按权限读取 Skill 说明，保留原始用户消息和旧 `/remember` 行为；目录发现包含获准的内置工具及 Skill 摘要，不借说明工具扩大原运行权限。

HITL 恢复可另行提交 `public_query`，仅限已授权公共 HTTP 工具的原运行。通用补充与公开查询分别保存，原运行快照不改写；公开字段沿用敏感配置检查并参与恢复幂等摘要。公共参数 Agent 仅接收最近一次明确授权的完整公开查询，通用补充不自动外发。原助手消息和 Run 保持不变。前端等待面板展示独立可选公开查询，切换会话/空间后清空。

部署预检核验当前目录与历史摘要、活动 Run/评测的冻结范围，旧任务不自动升级；即使对应任务开关已关闭，也检查仍存在的活动冻结任务。API readiness 检查能力目录与持久 Run 依赖，Worker 配置指纹包含能力开关及允许的 Secret 名称。

恢复/激活/评测及原 Run 回归 **64 passed**，预检组合 **23 passed、8 subtests passed**，前端构建和 **26 项 Node 测试**通过。证据 XML 位于忽略目录 `data/validation/stage7-activation-public-resume.xml` 和 `stage7-preflight.xml`。这些仍使用脚本模型/HTTP 替身；未对业务库或真实服务做阶段七部署验收。

当前仍未完成 SQL/Web 可管理配置、管理页面、能力中心与键盘入口、整阶段真实存储/浏览器/Docker/业务部署验收。业务环境仍未升级或启用阶段七，未重建部署镜像。本阶段保持未提交，不能把专项通过当作阶段完成。

本轮后端完整回归 **628 passed、47 skipped、38 subtests passed**（`data/validation/stage7-backend-intermediate.xml`）；跳过的 PostgreSQL 专项另行执行，上述 30 项通过不包括本阶段后续尚未实现的运行链。没有改写第五阶段真实 A/B 或第六阶段部署快照。

接入运行链后的完整回归为 **640 passed、47 skipped、38 subtests passed**（`data/validation/stage7-backend-execution.xml`）。随后增加 HTTP web 模式无 Tavily 入队及类型化快照嵌套字典再次校验，最新控制/运行专项见 `data/validation/stage7-capability-latest.xml`；这些后续边界变更不冒充已包含在前面的 640 项计数。Skill 说明和斜杠激活也仍待接入。

上述历史计数之后，评测冻结、Skill 说明/斜杠激活、公开 HITL 输入和预检均已进入本轮完整回归：**652 passed、47 skipped、38 subtests passed**（`data/validation/stage7-backend-activation.xml`）。前端构建和 **26 项 Node 测试**通过。仍按前文剩余清单继续阶段七，不提交中间阶段，也不将这些离线/替身验证宣称为最终部署验收。

能力中心、管理员表单与版本历史、命令面板和聊天激活已接入实际 API；前端构建及 **32 项 Node 测试**通过。独立 PostgreSQL 浏览器预览已验证创建 Tool/Skill、编辑 v2、Ctrl+K 搜索/回车、实际 Run 与版本引用、普通账号限制和团队切换清空选择，见 [界面中间验收](validation/stage7-capability-ui.md)。模型/HTTP 使用替身，预览 Worker 在线程内运行，不替代真实独立部署。SQL/Web 配置、Skill 指令参与上下文估算、完整 CRUD/错误路径及整阶段部署仍待完成。


SQL/Web 运行配置及 Skill 上下文估算现已实现。管理员可版本化设置 SQL 启用、独立只读连接的表白名单、来源名称、超时和行数上限，以及内置网页工具开关；连接凭据仍由服务端提供。启用 SQL 前复用真实只读角色/表检查，失败不提交目录与历史。Run 保存解析后的 SQL 策略和不含密码的连接身份摘要，执行创建专用客户端并关闭；数据库变更、撤销表权限或停用工具会阻止旧运行。修改行数等普通配置不改写已冻结策略，密码轮换不改变连接身份。

新增配置/激活/API 专项 **44 passed、8 subtests passed**；随后包含 SQL 客户端冻结/关闭的控制面专项 **26 passed**。完整后端 **657 passed、47 skipped、38 subtests passed**（`data/validation/stage7-backend-runtime.xml`），前端 **32 项 Node 测试**与构建通过。独立 PostgreSQL 迁移/并发 **30 passed**，新增真实 SQL 控制面专项与既有两项存储测试合计 **3 passed**（`stage7-runtime-sql-storage.xml`）：临时独立业务数据库和只读角色实际执行查询/截断，角色被授予 UPDATE 后配置核验拒绝，原修订不变，临时库和角色已清理。

浏览器在原隔离预览中保存未启用 SQL 配置至修订 5、停用网页工具至修订 6，能力中心立即显示不可使用；截图见 `docs/screenshots/stage7-runtime-controls.png`。这不是业务配置变更，也未将未配置的 SQL 标为可用。平台管理补齐 Skill/Tool 入口，命令面板跳过任务锁定项，历史请求避免旧响应覆盖新结果。最终 Docker 独立 Worker、完整 CRUD/错误场景以及业务备份/迁移/启用仍待完成，阶段七未提交。


2026-10-21 最终门禁已完成：完整后端 657 passed、48 skipped、38 subtests passed，独立 PostgreSQL 31 passed，前端 32 项及构建通过。真实 Docker 运行/撤权/取消/CRUD、浏览器冲突/停用、业务备份恢复/迁移/启用与数据保留已验证，详见阶段七最终验收。审批和 Sandbox 继续属于阶段八，企业 A/B 失败结论不变。
