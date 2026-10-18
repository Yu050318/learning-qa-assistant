# 第十一阶段：模型配置、检测和调用账本

> 本文保留早期两槽位模型中心的实现记录。第四阶段的新目录、角色、配置历史与升级流程见[模型目录与检索改写](model-catalog.md)；当前修复和验收状态见[补齐进度](replication-repair-progress.md)。

模型中心复用已有 DeepSeek/Ollama 适配器和 ModelRuntime。新增两张 PostgreSQL 表：一张保存当前配置及修订号，一张保存模型调用尝试。默认关闭，原 .env 模型选择和降级行为继续可用，不新增多 Agent 框架或依赖。

![模型配置界面（隔离预览）](screenshots/model-center.jpg)

## 配置与权限

```powershell
# 已有平台库上只创建两张附加表，并从当前服务端配置初始化一次
.venv/Scripts/python.exe -m app.bootstrap --model-center

# 在 .env 中设置，所有 API 进程保持相同配置并重启：
# MODEL_CENTER_ENABLED=true
# 管理员通过 ADMIN_INVITE_CODE 注册，旧白名单仅供显式迁移使用
```

普通已登录用户可查看当前模型资料、目录检测状态及自己的统计。只有持久化角色为 admin 的平台管理员可修改配置、发起检测和查看全局统计；组织所有者/管理员不自动取得平台管理权限。管理员注册与旧白名单迁移见[平台管理](platform-administration.md)。没有平台管理员时，页面保持只读。

页面管理当前两个接入槽位的模型名、启停、上下文窗口、默认提供商、单次模型超时、最大输出和 DeepSeek → Ollama 降级开关。继续使用原 `model_provider=deepseek|ollama` 接口，无需前端传入连接地址或凭据。Embedding/Rerank/MinerU 继续由服务端配置，特别是 Embedding 关联已有索引维度，本阶段不在页面提供在线换索引模型。

连接地址和密钥只读自服务端，不写入配置表、不发送浏览器、不接受客户端覆盖。启用云模型时必须已有服务端凭据；默认模型必须启用，降级要求两端都启用，窗口需为输出预留至少 1024 Token。Ollama 将配置窗口传为 `num_ctx`，两类模型都沿用原 SDK 的输出和超时参数；配置窗口只是声明与请求参数，不会扩大远端模型本身的容量。

保存需要 `expected_revision`，数据库行锁与修订号比较防止多人覆盖。所有 API 进程从同一个平台库读取最新配置，新问答在执行开始时冻结 Settings/Router 快照，整个 Supervisor、子 Agent、修复和降级保持同一修订号；最终消息记录 `model_configuration_revision`。正在执行的问答不会被后续编辑切换模型。上下文估算、摘要压缩、聊天选择框和 capabilities 同步使用中心配置。

配置启用后，.env 中的模型名等只作为首次种子，后续以数据库配置为准；重复 bootstrap 不覆盖已有配置。连接地址、密钥仍以服务端配置为准；管理权限以数据库用户角色为准。修订号用于并发控制和调用追踪，本阶段没有配置历史回滚界面。

## 可用性检测

检测由管理员显式触发，只查询模型目录：DeepSeek 使用 [`GET /models`](https://api-docs.deepseek.com/api/list-models/)，Ollama 使用 [`GET /api/tags`](https://docs.ollama.com/api/tags)。不发送生成提示，不拉取、安装或删除模型，不跟随重定向。请求最多 5 秒，页面只显示状态和安全错误码，不显示原始响应错误。

目录必须包含所配置的模型名。状态包含 unknown/available/unavailable/disabled/not_configured，结果有效期 5 分钟；配置修改会失效旧检测结果。如果检测过程中配置修订号变化，拒绝保存旧结果，要求重新检测。

`capabilities.models[].available` 继续表示模型已启用且具备本地必要配置，可以选择；新增 health 区分实际目录检测状态。未检测不意味着在线可用，目录检测通过也不证明生成、工具调用、上下文容量或答案质量通过验收。

## 调用统计与复用

ModelRuntime 的原调用预算、暂时故障判断和一次降级策略保持不变。每次实际模型调用前，观察器先创建 started 记录；成功或失败后更新状态、耗时、提供商、模型名、修订号、尝试序号、是否降级，以及模型实际报告的 Token。失败的主模型和成功的降级模型分别记录。摘要压缩的直接 ModelRouter.generate 也使用相同观察器，不重复实现一套降级策略。

如果调用前记录无法保存，不继续发出模型请求；如果模型已调用但结果统计保存失败，保留 started/未知状态并报错，不因为统计故障再次调用收费模型。进程被强杀时，started 表示未完成或结果未知，不能据此推断是否计费。未知 Token 保持 null，前端同时显示已报告部分与覆盖次数，不把缺失用量填成零。

![调用账本与用量覆盖（隔离样例）](screenshots/model-center-statistics.jpg)

统计由数据库按提供商/模型聚合，支持最近 7/30/90 天；普通用户只查本人，平台模型管理员可选择全部用户。模型调用 succeeded 仅表示该调用返回，后续引用校验、权限检查或最终消息保存仍可能失败。统计不是供应商账单，未覆盖 Embedding/Rerank/MinerU、独立 CLI 评估或平台外的调用。调用记录不包含问题、提示、模型响应正文、源片段或凭据；删除会话会级联删除对应记录，因此这里也不是不可删除的计费总账。

## 接口与代码位置

| 方法 | 路径 | 权限与内容 |
| --- | --- | --- |
| GET | `/api/v2/model-center` | 已登录；安全配置、检测状态、can_manage |
| PUT | `/api/v2/model-center` | 管理员；expected_revision + 完整配置 |
| POST | `/api/v2/model-center/probe/{provider}` | 管理员；显式目录检测 |
| GET | `/api/v2/model-center/statistics?days=30` | 当前用户调用聚合 |
| GET | `/api/v2/model-center/statistics?global_scope=true` | 管理员；全部用户调用聚合 |

服务位于 `app/application/model_center.py`，复用 `providers.py` 和 `ModelRuntime`；前端 `ModelsView.vue` 提供管理与只读视图。模型名、启停和预算变化不修改私有资料、Milvus 索引、记忆或已有引用。

## 验证与回退

```powershell
.venv/Scripts/python.exe -m pytest tests/test_model_center.py -q
.venv/Scripts/python.exe -m pytest -q
cd frontend
npm run build
npm run test:agents
```

离线测试覆盖管理员越权、配置修订冲突、输入约束和凭据字段拒绝、目录替身、停用、主模型失败后的降级记账、未知用量、用户隔离、统计失败阻止调用、统计失败后不重新生成，以及真实 Supervisor 在配置并发修改时仍使用原快照。所有收费服务使用替身。

```powershell
# 只用没有已有模型配置行的专用隔离库，名称必须包含 test
$env:RUN_MODEL_CENTER_INTEGRATION='1'
$env:MODEL_CENTER_TEST_DATABASE_URL='postgresql+psycopg://TEST_ROLE:TEST_PASSWORD@localhost/rag_model_test?sslmode=require'
.venv/Scripts/python.exe -m pytest tests/integration/test_model_center.py -q
```

真实 PostgreSQL 用例验证配置行锁、旧修订拒绝、记录状态、未知用量、聚合和初始化不覆盖。浏览器预览使用临时 SQLite、真实登录与权限接口、模型目录替身和合成调用记录，验证管理员保存/检测、普通用户只读和统计隔离；不代表真实模型服务验收。测试预览后来使用独立 `.localhost` 域名隔离 Cookie，并清理预览服务、浏览器标签和临时数据库。

回退时设置 `MODEL_CENTER_ENABLED=false`、重启全部 API，恢复原 .env 路由和模型名；两张表及已有调用记录保留。停用期间不新增中心统计。真实服务连通、推理及全项目联合验收仍由后续阶段验证。

2026-10-16 验证记录：最终后端全量 **313 passed、14 skipped、38 subtests passed**；模型中心专项 **14 passed**；前端 vue-tsc/Vite 构建及 **8 项 Node 测试**通过。真实 PostgreSQL 18 临时容器 `codex-model-pg-31950c88` 中的模型中心集成测试 **1 passed in 7.68s**，容器及临时数据库已清理，没有连接或升级现有平台库。浏览器已验证管理员保存到新修订、目录替身检测、用量未知展示和普通用户只读/个人统计隔离；未做真实模型生成验收。bootstrap help、`uv lock --check`、diff 空白检查通过，23 个源码/文档变更未命中当前配置的长密钥值。
