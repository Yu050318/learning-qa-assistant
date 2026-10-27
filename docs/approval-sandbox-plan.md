# 阶段八：Run 审批、隔离执行、产物与追踪

开始条件已核对：阶段七 `a2a63415d3a7fd97f0a30d8468ed4898ede08192` 已推送 GitHub，远端 main 与本地一致。本文件是实施及验收清单，不是阶段完成证明。

## 参考与现有边界

参考本地 SuperMew `eb0495e` 的 `backend/guardrails/approvals.py`、`backend/api/routes/runs.py`、`backend/sandbox/{contracts,runtime,docker}.py`、`docker/sandbox/{Dockerfile,runner.py}`，以及前端 `Capabilities/ApprovalDialog.vue`、`Artifacts/ArtifactShelf.vue`、`Run/ExecutionTimeline.vue`、`Chat/KnowledgeContextPanel.vue`。

参考的 Run 预授权仅允许平台管理员批准已声明需要审批的工具名称，绑定 user/tenant/thread/run，不是跨用户审批台。其 Sandbox 使用固定镜像入口、stdin 协议、无网络、无 bind mount、非 root 用户及资源限制；原 Sandbox 只返回 stdout/stderr 与文件计数，不导出文件。产物组件属于独立展示能力，不能从这些文件推断已有完整 Sandbox 文件下载。

当前项目已有持久 Run、事件、租约、HITL、工具检查点和冻结能力目录。阶段八沿用这些链路；不新增平行的问答任务系统，不将审批当作 ACL、SQL 白名单、公共输入、预算或沙箱边界的绕行机制。旧请求没有审批字段时保持原幂等语义；默认关闭的新能力不能改变阶段七已部署行为。

兼容性门禁：新增内置工具不得重新解释旧目录中的同名自定义 HTTP Tool，也不能因新默认字段改变阶段七目录摘要。必须保留旧快照和历史的解析/摘要语义；启用新工具遇到名称冲突时明确拒绝配置，不自动覆盖、改名或重写历史。新增批准字段的空默认值不改变旧请求指纹。

## 实施范围

1. **绑定授权**：创建 Run 时携带明确的批准工具名称及所确认的目录修订。服务端只接受平台管理员、当前获准且声明 requires_approval 的工具，冻结 user/session/workspace/run 和目录摘要；未知、重复、越权或过期目录拒绝入队。批准不能复用于其他 Run、空间或版本。HITL/接管保留原授权，角色撤销、工具停用或功能关闭仍在执行和最终发布前复核。
2. **审批界面与执行**：能力中心展示需要确认的工具、网络及资源范围；发送前的可访问对话框允许取消并保留草稿，确认只用于本次提交，切换账号/空间/会话后清空。未批准的高风险工具不进入模型可调用集合。HTTP Tool 驱动也核对绑定授权，不通过传入一个布尔值跳过检查。评测默认不代替用户批准高风险工具。
3. **Sandbox**：可信 Run Worker 调用固定 Docker 适配器，不在 API 或宿主进程执行生成代码。不创建另一套 Sandbox 问答 Worker；隔离边界是每次独立的工作负载容器。仅 Python/sh 有界源代码；本地已有镜像必须按 digest 固定并校验 runner/用户配置，禁止执行时拉取镜像。固定无网络、无宿主挂载、只读根目录、非 root、去能力、no-new-privileges、私有 PID/IPC、tmpfs、CPU/内存/进程/文件/输出/时间上限。不传入宿主环境或模型/数据库凭据。只有显式 Sandbox 部署给可信 Run Worker Docker 访问，API 不挂载 Docker socket。
4. **取消与恢复**：沿用工具计数、Run 剩余预算和 owner fencing。stdout/stderr 与协议读取有界；超时、取消、租约丢失和退出均清理本次命名容器，清理失败明确报告。成功回执可重放；无法确认的代码执行不自动重跑。并发和遗留容器核查只处理本应用标识，不能全局删除容器或镜像。
5. **产物链路**：持久记录 Run/用户/空间归属、工具版本、大小、摘要和媒体类型；提供鉴权列表、读取/下载和界面展示。产物通过受控协议显式导出，不挂载宿主目录；限制总数/单项/总量，拒绝路径穿越、链接、特殊文件和内容不符。预览仅安全文本和校验过的图片，HTML/SVG 等不能在应用源中执行。跨用户、跨空间及撤权后的获取拒绝，重复回执不重复创建产物；取消/失败/清理保持一致。
6. **完整追踪**：按真实 Run/Event 展示时间线、工具开始/结束/失败、审批决策、恢复/取消、预算、产物和 RAG 上下文/引用边界。刷新与 SSE 重放不重复，缺少历史事件时明确提示；不把工具调用、未确认结果或中间片段显示成最终成功，不展示密钥/原始驱动异常。

## 验收门禁

- 域/API：绑定完整性、未知/重复批准、普通账号拒绝、目录 CAS、跨 Run/会话/空间/版本拒绝、撤权、无审批调用失败、幂等与旧请求兼容。
- 实际运行：HTTP approval-only 与 Sandbox 同一 Run 的冻结、调用、取消、HITL 和接管；成功不重复，未知结果不自动重跑；审批不能扩大模式或资料权限。
- Sandbox 真实容器：成功 Python/sh、网络/宿主文件不可达、非 root/只读根、资源限制、输出洪泛、超时/取消、异常协议、符号链接/目录穿越、失败清理和镜像不匹配；保留原数据库与向量卷。
- 产物：真实创建/读取/下载、摘要与媒体一致、越权拒绝、主动内容无法在应用源执行、重放去重与生命周期。
- 浏览器：取消/确认、键盘/焦点、权限、草稿/空间切换、真实执行及产物、时间线与 RAG 上下文、错误和重放。
- 完整回归、真实 PostgreSQL、独立 Docker API/Run Worker、业务备份/预检/受控部署；保留阶段五 A/B 失败结论。按用户 2026-10-22 最新要求，阶段八验收、提交推送并清理本次废弃镜像后结束目标，不进入阶段九。任务收尾仅删除本次生成且不再使用的 Docker 镜像，保留最终部署镜像和已有数据卷。

## 当前状态

最新业务验收（2026-10-27）：用户恢复数据库服务后，已完成新的业务备份及实际恢复验证，再迁移至 0010。API/前端与三个独立 Worker 全部通过预检；旧账号角色、密码摘要、能力历史、925 个绑定叶块及文档/会话数量保持不变。只开启 TOOL_APPROVALS_ENABLED，业务 Sandbox 保持可选部署/default off；完整链路在独立 Docker 开发部署验收通过。提交推送与最终镜像清理收尾后结束目标，不进入阶段九。见 [业务部署](validation/stage8-deployment.json)。以下保留实施历史，旧阻塞描述已被本段解决。

最新验收：独立 Docker 服务的真实 HTTP + Sandbox + 同次 Run HITL、浏览器公开查询确认/刷新恢复、真实 RAG 叶块/父块追踪、整 Run Worker SIGKILL 后接管与孤儿回收均已通过。后端 **775 passed、52 skipped、38 subtests passed**，前端 **45 项测试及构建**通过。旧阶段七备份恢复升级至 0010 且临时库已清理。业务 PostgreSQL SSL 连接仍失败，尚无当前业务库的新验证备份，因此业务迁移/部署、阶段提交推送和最终镜像清理仍待完成；第八阶段未标记完成。见 [独立部署证据](validation/stage8-docker-acceptance.md)。以下保留历史快照。

运行追踪现已实现并完成真实 Sandbox 运行的浏览器中间验收；成功运行保存最终检索检查点，权限变化隐藏上下文。最新完整后端 **773 passed、52 skipped、38 subtests passed**，PostgreSQL **4 passed**，前端 **45 项测试**及构建通过。[运行追踪证据](validation/stage8-run-trace.md)。真实检索内容浏览器验收、混合工具/HITL 与整阶段部署仍待完成；以下保留历史快照。按最新用户要求，第八阶段提交推送及清理后结束目标，不进入第九阶段。

2026-10-22 最新进展：产物页面和数值限额授权弹窗已通过独立 API/Run Worker 浏览器验收；已增加本部署限定的遗留容器核查与 Run 行锁保护，并以真实 Docker 驱动进程 SIGKILL、实际 90 秒宽限和 PostgreSQL 证明保护/回收边界。最新完整后端 **768 passed、52 skipped、38 subtests passed**，相关专项 **80 passed**，真实 PostgreSQL **4 passed**；前端 **42 项测试**及构建通过。见 [产物界面](validation/stage8-artifact-ui.md)、[异常退出治理](validation/stage8-sandbox-reaping.md)。完整追踪、混合工具/HITL、整阶段 Worker 重启与业务部署仍待完成，尚未提交或推送。以下是实现过程的历史快照。

最新中间验收：Sandbox 已接入同一持久 Run 的工具检查点与产物存储，真实 Docker/PostgreSQL 验证 **3 个文件入库、下载摘要、安全预览、未登录/跨用户拒绝及运行中取消清理**。模型为脚本替身，HTTP 为进程内 TestClient；不代替独立服务部署、生产 rootless 或模型质量验证。当前后端全量 **755 passed、51 skipped、38 subtests passed**，真实 PostgreSQL 专项 **4 passed**。见 [Run 与产物中间验收](validation/stage8-sandbox-run.md)。产物界面、完整追踪、意外退出容器核查及整阶段部署仍待完成。以下段落保留逐步实现时的专项记录。

已接入 Run 创建、冻结批准、持久批准事件、执行和恢复复核，以及 HTTP 驱动的绑定验证。平台管理员才能批准当前声明需审批且属于本次 Skill/模式的工具，目录变化要求重新确认；缺少批准的高风险工具不会进入自动调度。默认关闭批准时保持旧请求指纹、目录摘要和 Worker 指纹。

前端加入原生确认对话框，取消保留草稿；确认内容只进入这次 Run，角色/会话/空间/草稿变更清空待确认状态。隔离 PostgreSQL 浏览器验证取消无 Run、确认执行、再次确认、并发旧目录拒绝与草稿恢复，见 [审批中间验收](validation/stage8-approval-ui.md)。完整后端 **683 passed、48 skipped、38 subtests passed**，前端 **35 项 Node 测试**及构建通过。

Sandbox 已实现有界 source、固定语言、不可变镜像、可信 runner 核验、容器驱动及资源预算。契约与驱动组合 **34 passed**，Docker Desktop 开发环境 **20 项真实容器检查**通过，含 Python/sh、资源边界、危险文件拒绝、取消/租约丢失和容器清理；生产默认 rootless 门禁明确拒绝该开发 daemon。见 [驱动中间验收](validation/stage8-sandbox-docker.md)。同一次 Run 的 Sandbox 调度、产物、完整追踪和整阶段部署仍待完成。没有迁移或更改阶段七业务配置，本阶段不提交中间实现。

已补 Sandbox 版本化策略和管理员运行配置 API，沿用同一目录修订及不可变历史。镜像必须是 digest，资源范围使用既有有界契约。策略注册后名称不能被 HTTP Tool 覆盖；旧 HTTP 同名和已退休同名均阻止策略注册。未配置 Sandbox 的目录序列化省略新增空字段，保持旧目录摘要和历史形状。默认关闭 Sandbox 时 Worker 摘要不变；启用时区分协议和 rootless 要求。预检拒绝缺少依赖开关或策略的配置。

Sandbox 工具只进入已明确批准的管理员自动模式，公共查询不是其调用前提；Run 创建会冻结策略版本、镜像、资源范围和本次授权。公开目录提供待确认的策略版本与资源上限。更新策略不重写已创建的运行；停用/关闭功能和角色撤销会拒绝范围复核。相关目录/API/冻结专项 **8 passed**，能力声明、管理、工作流、预检、审批、Sandbox 契约与驱动组合 **137 passed**，见 `data/validation/stage8-sandbox-catalog-regression.xml`。这是持久冻结与授权验收，尚未证明 Supervisor 已调用 Sandbox 或产物已持久化，不能据此启用业务功能。
