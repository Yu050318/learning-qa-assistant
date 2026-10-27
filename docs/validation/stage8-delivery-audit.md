# 第八阶段交付核查

本清单依据 `approval-sandbox-plan.md` 的六项实施范围及验收门禁核对。结论：**实现、隔离验收、新备份恢复验证与业务部署完成；提交推送、最终镜像清理及目标状态仍须收尾。**

| 需求 | 可核查证据 | 结论与边界 |
| --- | --- | --- |
| 1. 管理员审批绑定用户/会话/空间/Run/目录；CAS、撤权、旧请求兼容 | `test_tool_approvals.py`、`test_run_tool_approvals.py`、`test_http_tools.py`；全量测试 XML；独立混合 Run SQL | 已验证；API 拒绝无效授权，真实 v4 Run 不随目录 v5 更新 |
| 2. 确认/取消、保留草稿、公开输入和预算展示、评测不自动批准 | approval-ui、artifact-ui；前端 tool-approvals 测试；stock Docker 浏览器审批及真实 echo | 已验证；早期浏览器证据含脚本模型，最终混合流使用真实提供商 |
| 3. 固定镜像/runner、Python/sh、无网络挂载/宿主秘密、非 root/只读根、资源限制 | sandbox-docker 的 20 项真实容器检查；driver/contracts 测试；Docker 镜像和挂载证据 | 已验证 Docker Desktop 开发环境；生产 rootless 默认拒绝不合规 daemon，未声称生产 rootless 部署通过 |
| 4. 取消、剩余预算、owner fencing、明确清理错误、未知结果不重跑、限定回收 | sandbox-run/artifact-ui 真实停止；sandbox-reaping；stock 整 Worker SIGKILL + SQL；真实 PostgreSQL 行锁测试 | 已验证；崩溃 Run generation=2、非只读 attempt=1、uncertain、无产物，孤儿容器已回收 |
| 5. 有界持久产物/归属/摘要、原子回执、权限/撤权、文本/PNG 预览、主动内容附件 | run_artifacts 域/API/真实 PostgreSQL 测试；artifact-ui 四文件；stock 两次单文件下载及 SQL | 已验证；历史证据明确保留模型替身与源代码覆盖等环境边界 |
| 6. 真事件时间线、分页/重放/旧数据提示、RAG 叶块与父块、预算、权限及脱敏 | run_trace 域/API/前端测试；run-trace；真实 Milvus/Dashscope/DeepSeek stock RAG 浏览器 | 已验证；六个叶块与一个父块，父块仅供理解、无独立引用入口 |
| 完整回归、独立 API/Worker、真实 PostgreSQL | backend-final.xml：775 passed / 52 skipped / 38 subtests；前端 45 项及构建；此前 PostgreSQL 专项 4 项；stock 容器源文件与宿主 107 项一致 | 已验证；52 个跳过项不是通过项，真实数据库专项和外部服务由单独验收补充 |
| 阶段五质量报告不被覆盖 | 原始 stage5-enterprise-live-ab JSON/Markdown；进度文档与本次报告 | 原质量门禁失败结论保留；虚构验收手册不能替代企业 80 题质量评测 |
| 当前业务库新备份 + 恢复验证 + 受控升级/预检部署 | business-connection.json；restore-rehearsal.json | **已验证**：用户恢复服务后完成新备份和实际恢复，业务升级至 0010、完整预检及 API/UI 均就绪；角色/密码/历史和索引保留，只有工具审批开关改变。业务 Sandbox 默认关闭，Docker 开发部署单独验证。详见 stage8-deployment.json |
| 第八阶段提交并推送 GitHub、远端核验 | 本地 HEAD 仍是阶段七 a2a6341，阶段八工作区未提交 | **未完成**：阶段完整验收之前不推送未完成交付 |
| 删除本次生成且已不用的镜像，保留最终镜像/旧数据库/卷 | 阶段八镜像基线与当前镜像/容器库存；最终清理待部署后执行 | **未完成**：保留当前 stock API、Run Worker、前端和固定 Sandbox 镜像；不用全局 prune |
| 完成第八阶段后结束目标，不进入第九阶段 | 计划与进度文档的终止范围 | 收尾完成后执行；当前不标记 complete，不开始阶段九 |

更细的运行环境、失败探索记录和证据链接见 [独立部署报告](stage8-docker-acceptance.md)。本次核查未再次调用付费模型、未重跑企业 A/B、未改写历史验收证据。
