# 模型目录与检索改写（第四阶段）

模型中心支持一个服务端连接下的多个 Profile，可分别分配回答与工具调度（Answer）、快速规划与改写（Fast）、证据评判（Grader）、质量评测（Evaluator）。沿用 DeepSeek/Ollama 适配器；连接地址、密钥由服务端管理，页面不能提交或重定向凭据。Evaluator 已有独立路由与记账，评测任务和独立 Worker 在第五阶段交付。

## 升级和使用

先备份已有数据库及本地文件，执行 `.venv/Scripts/python.exe -m app.migrate` 到 `0005_model_catalog_history`。该迁移保留原配置、原修订号和旧调用记录，新增配置历史及调用角色/Profile 字段；旧调用的未知角色保留为空。随后可执行 `bootstrap --model-center` 初始化空配置。附加表必须先通过迁移创建，bootstrap 不再自行以最新 ORM 改写旧模型表。

旧两槽位配置保持兼容，管理员首次在“模型”页面保存目录后转为新目录格式。转换时默认四角色沿用旧默认模型；管理员可新增模型，再分别分配各角色。旧客户端的提供商默认模型在折叠面板设置。新目录保存后，旧配置写接口拒绝覆盖，避免丢失角色和额外 Profile。

每次保存需要当前 `expected_revision`，配置与历史原子提交；已有 Profile 的参数变化使其版本递增，角色重分配改变目录修订号。删除前须解除角色、旧客户端默认模型和降级引用；删除过的 ID 不得重新使用。降级最多一跳，目标必须启用并满足使用角色的能力约束，同一提供商下可降级到不同模型。

回答角色要求流式及工具能力，其余角色要求结构化输出。能力勾选是配置声明，目录检测只检查模型是否列出，不证明推理和工具能力。检测绑定确切 Profile 与目录修订；检测期间配置变化则拒绝保存过期结果。普通用户可查看目录和个人用量，修改、检测、历史和全局统计均需平台管理员。

## 运行与统计

聊天的回答模型选择器使用 Profile ID，因此同一提供商的模型不会合并。`model_profile_id` 与兼容字段 `model_provider` 互斥；问答、上下文估算和摘要压缩共用此选择约束。

创建 Run 时冻结完整目录、修订及选定回答 Profile，接管与人工恢复继续消费原快照。回答、规划和评判按各自窗口限制输入，共用整轮总调用预算、用量和检查点。模型角色、Profile ID/版本进入调用账本和消息元数据；同一提供商的多模型及降级不会混淆。历史调用未记录身份的部分保持未知，不回填猜测。

## Step-back / HyDE

Grader 在证据不足时选择普通改写、Step-back 或 HyDE，由 Fast 角色输出相应的结构化结果。Step-back 补充上位背景问题，HyDE 生成仅供 dense embedding 使用的假设文本；两者都保留原问题约束，沿用一次补检索上限、授权文档范围和整轮模型预算。假设文本不进入真实证据、引用或 Grader 的来源内容。已确认的改写、检索和模型结果在接管时复用。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v2/model-center/catalog` | 读取目录、角色和检测状态 |
| PUT | `/api/v2/model-center/catalog` | 管理员原子保存完整目录 |
| POST | `/api/v2/model-center/catalog/profiles/{profile_id}/probe` | 管理员检测指定模型目录 |
| GET | `/api/v2/model-center/versions?offset=0&limit=10` | 管理员分页查看历史 |
| GET | `/api/v2/model-center/statistics` | 按角色、Profile 版本、提供商和模型聚合 |

## 验证证据

浏览器使用独立 PostgreSQL、实际 API/Run Worker 与本地脚本模型；目录调用使用 HTTP 替身。验证新增第三个 Profile、独立 Fast 分配、修改后版本递增、保存后历史刷新、指定 Profile 检测、聊天实际选模和普通用户只读；数据库同时核对冻结快照、原消息对及精确调用身份。见[脱敏证据](validation/stage4-browser-models.json)、[模型目录](screenshots/model-catalog-stage4.png)、[选模问答](screenshots/model-selection-stage4.png)、[普通用户](screenshots/model-readonly-stage4.png)。这些证据不代表真实上游推理质量。
