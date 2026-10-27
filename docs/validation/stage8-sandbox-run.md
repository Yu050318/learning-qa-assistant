# 阶段八 Sandbox Run 与产物中间验收

2026-10-21，真实 Docker 容器、PostgreSQL 和持久 Run 完成执行与文件下载链路。结构化证据及源码/镜像摘要见 [执行记录](stage8-sandbox-run.json)。这是第八阶段中间验收，不代表界面或整阶段部署完成。

## 实际链路

- 在可信、带 Docker CLI 的 Run Worker 镜像内运行验收入口 `tests/sandbox_run_docker_acceptance.py`，创建并迁移独立临时 PostgreSQL 数据库。没有携带业务配置或模型凭据。
- 使用脚本模型生成工具调用，实际 Supervisor、检查点、租约、批准复核和 Docker 驱动均参与执行。没有将脚本模型当作真实供应商或问答质量测试。
- 入队后更新当前 Sandbox 策略至 v2；Run 实际执行和产物记录保留已确认的 v1。工作负载容器隔离执行 Python，返回文本、PNG 图片和 HTML 附件共 **3 个文件**，文件经受控协议入库，没有宿主文件挂载。
- 工具回执确认前产物不可见；确认时，回执和 `artifacts_ready` 事件同事务提交。成功检查点可重放，已确认运行再次领取不会产生新容器。产物记录保存 Run、用户、会话、团队范围、工具版本、大小、摘要和媒体类型。
- 真实 HTTP 路由适配器验证列表、原文件下载及安全预览。三个下载的 SHA-256 与数据库回执一致；PNG 解码后以无元数据的新 PNG 预览；HTML 为附件且预览返回 415；未登录返回 401，跨用户返回 404。此处 HTTP 使用进程内 TestClient，不能代替独立 API 容器的网络部署验收。
- 第二次真实 Run 在观察到工作负载容器运行后取消，Worker 将其清理并结算为 `cancelled`，不发布产物。两次运行对应容器均无残留，临时数据库在验收结束时删除。
- 当前是 Docker Desktop 的显式开发配置 `sandbox_require_rootless=False`；生产默认仍要求 rootless，不将开发环境通过作为生产 rootless 证明。

## 存储与恢复专项

- 产物专项最初 **22 passed**：待确认文件不可见、重复回执不重复创建、名称/文件数/大小/批次校验、跨用户/跨 Run、内容摘要改变、取消/失败隐藏、撤权和团队成员撤销、会话删除级联、非主动预览、PNG/JPEG/WebP 解码和文本预览上限。
- Run 调度专项 **6 passed**：实际 Supervisor/Run Worker 配合受控 Docker 替身，验证成功引用及产物、成功回执接管复用、未知执行结果禁止自动重跑、取消时清理失败仍作为明确错误、执行失败不注册事实证据、无效语言在执行前拒绝。替身专项不是额外的真实容器证据。
- 真实 PostgreSQL 专项及空库重复迁移合计 **4 passed**：从 0009 升级至新增 `0010_run_artifacts` 保留原账号和目录；bytea 与长度/外键约束；事件写入失败整体回滚，恢复提交后可见；真实 Run 行锁串行化取消与产物写入。报告 `data/validation/stage8-artifact-storage.xml`。
- 既有 Run、HTTP Tool、批准、Sandbox 目录及产物组合 **69 passed**（`data/validation/stage8-artifacts-workflow-regression.xml`）。随后补充运行总量配额及只读/取消回执写入拒绝检查，纳入下一次完整回归，不把先前计数当作新增检查的结果。
- 随后后端全量 **755 passed、51 skipped、38 subtests passed**（`data/validation/stage8-backend-artifacts.xml`），包含上述两项新增拒绝检查；产物专项范围因此为 24 项。51 个跳过项保留外部服务专项要求，不能把离线全量回归当作真实服务验收。
- 已增加单次与整 Run 文件/总量上限；待确认文件也计入配额，失败和取消后仍保留审计记录但不能获取。数据库为所有 Worker 共享，未新增并行的 Sandbox 问答 Worker 或静态文件 URL。

## 尚待完成

产物和审批资源范围界面、完整执行时间线/RAG 上下文、Worker 意外退出后的容器核查、同一 Run 的 HTTP 批准工具与 Sandbox 混合/HITL 验收、独立 API/Run Worker 部署及浏览器全链路验收、业务备份/迁移和最终阶段回归仍待完成。生产 rootless 运行尚未实测。当前没有升级业务数据库、启用业务 Sandbox 或推送阶段八中间源码，阶段九未开始。

真实 PostgreSQL 首次验证遇到 Docker Desktop 停止，旧测试端口失效。恢复后核对新端口并停止仅针对旧端口的本次测试调用，独立数据库专项重新执行通过；没有删除业务容器、数据卷或通过修改管道权限绕过限制。任务收尾仍仅清理本次产生且不再使用的镜像。
