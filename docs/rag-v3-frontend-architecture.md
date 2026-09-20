# Learning Q&A Assistant V3 前端架构与后端接入设计

日期：2026-09-14。状态：V3.0 已实施；本文同时作为实现契约与维护说明。

修订：保留 12 个源文件的精简目录；补齐新对话估算、上传恢复、资料可用性、模块调用规则、多模型指标和新增接口契约。

本文依据当前 `app/` 中的接口和已选界面制定。V3 是产品迭代名称，前端默认使用现有 V2 Agent 问答，不要求把所有接口改成 `/api/v3`。文中明确标为“拟新增”的接口目前不可调用。

## 1. 产品范围与界面基准

完整 MVP 包含对话、资料管理、文件夹批量上传、引用查看、模型选择，以及缓存命中率与上下文占用展示。默认使用最新已接入的 Agent 版本；页面不出现 V1/V2 切换，升级由应用发布控制。旧 V1 会话不混入 V2 列表，也不自动迁移。

采用已选的米白背景、深绿色主色、窄导航栏、细分隔线和中文排版。参考图保存在项目中：

| 画面 | 本地基准 | 职责 |
| --- | --- | --- |
| 对话 | [对话概念图](design/v3/chat-concept.png) | 会话列表、回答、引用、资料范围、输入与用量 |
| 资料 | [资料库概念图](design/v3/documents-concept.png) | 上传入口、文件搜索、状态筛选、多选删除 |
| 上传抽屉 | [文件夹上传概念图](design/v3/folder-upload-concept.png) | 批量文件处理与逐项结果 |

图是视觉基准，不是接口事实。落地时修正以下细节：

- 上传抽屉背景里残留的“已解析/解析中”统一改为用户要求的三种上传状态。
- 图中的 64%、18.6k/24k、文件数和大小是示例，必须替换成真实数据；缺失数据显示“—”或“暂不可用”。
- “全部取消”改为“停止剩余上传”：现有后端没有取消已接受解析任务的接口。
- 图中会话标题编辑需要新增接口；引用箭头先打开片段详情，当前没有原文件在线预览接口。
- 输入框附件按钮用于选择已上传资料；新增文件统一从左侧“资料”进入。

### 1.1 页面与路由

| 路由 | 页面 | 主要行为 |
| --- | --- | --- |
| `/chat` | 新对话 | 选择资料范围和模式，首次发送前创建会话 |
| `/chat/:sessionId` | 历史对话 | 加载消息、继续问答、查看引用和运行用量 |
| `/documents` | 资料库 | 搜索、筛选、上传、重试、批量删除 |

上传进度、引用详情、资料选择、删除确认和上下文说明采用抽屉/弹窗，不新增独立页面。窄屏收起会话列表和导航，资料表优先保留文件名、状态与操作。

### 1.2 视觉与交互约束

- CSS 变量统一背景 `#F3EFE7`、内容面 `#FFFDFA`、主色 `#245B45`、文字 `#242724`、成功绿、进行中橙、失败红。
- 以 1440px 桌面宽度为参考；导航约 88px，会话列表约 300–340px，正文自适应，长回答保持舒适行宽。
- 正文约 15–16px，行高 1.6–1.8；控件约 40px 高；圆角 6–10px，主要依靠留白和分隔线。
- 中文使用系统字体栈。图标统一采用一个图标库，避免混用 emoji。
- 输入支持 Enter 发送、Shift+Enter 换行；中文输入法正在组合文字时不发送。
- 弹窗有焦点管理和键盘关闭，多选框、图标按钮具备可读标签；状态同时用文字表达。

## 2. 前端技术与分层

建议 Vue 3 + TypeScript + Vite + Vue Router + Pinia。Vue 官方工具链提供 Vite 与 TypeScript 的配套路径；Pinia 用于跨页面状态共享。[Vue 工具链](https://vuejs.org/guide/scaling-up/tooling)、[Pinia 文档](https://pinia.vuejs.org/introduction.html)。

请求默认使用原生 fetch；只有文件字节上传进度需要 XMLHttpRequest。视觉以 Vue 单文件组件和普通 CSS 实现。Markdown 使用成熟解析器，关闭原始 HTML，并限制链接协议；不把模型输出直接传入 `v-html`。无需另建 BFF、通用仓储框架或全局事件总线。

```text
Vue 页面与组件
       ↓ 用户操作 / 响应式数据
store.ts（跨页面状态） + 页面内局部逻辑
       ↓ 类型化调用
api.ts（请求、类型与业务接口）
       ↓ 同源 /api 请求
开发：Vite 代理；部署：反向代理
       ↓
FastAPI：文档 V1 API + 会话/Agent V2 API
       ↓
现有应用服务 → PostgreSQL / Milvus / MinerU / 模型 / Tavily
```

当前目录共 12 个 src 源文件（含演示数据），仅保留 views 和 components 两个分类目录。工程配置、入口 HTML、锁文件和必要测试不计入这个数量：

```text
frontend/
  src/
    main.ts                  启动 Vue、Pinia 与路由
    App.vue                  导航、用户入口、路由容器、全局上传抽屉
    router.ts                三个路由及默认跳转
    api.ts                   请求封装、接口类型、会话与文档 API
    store.ts                 一个 Pinia store，管理身份、会话、资料共享状态
    uploads.ts               上传队列、并发、进度、轮询与停止调度
    style.css                主题变量、基础样式与公共布局
    mock.ts                  演示数据与模拟接口行为
    views/
      ChatView.vue           会话列表、消息布局、输入、引用抽屉、资料选择、用量
      DocumentsView.vue      搜索、筛选、表格、多选删除与上传入口
    components/
      MessageContent.vue     Markdown 安全渲染与引用点击
      UploadDrawer.vue       批量上传抽屉与逐项结果
  package.json
  vite.config.ts
```

### 2.1 合并规则与必要边界

| 原拆分 | 精简后 | 理由 |
| --- | --- | --- |
| client/types/chat/documents 四个 API 文件 | api.ts | 接口数量有限，类型与调用就近查看 |
| identity/chat/documents 三个 store | store.ts | 一个应用级 store，按身份、会话、资料分段组织 |
| 上传 store + 轮询 composable | uploads.ts | 队列和轮询共用生命周期，避免重复轮询 |
| AppNav | App.vue 内 | 导航只使用一次 |
| SessionList/ChatComposer/CitationDetails/DocumentPicker/UsageIndicator | ChatView.vue 内 | 属于同一对话页面，不为每个区域建文件 |
| DocumentTable | DocumentsView.vue 内 | 搜索、筛选、选择与表格在同一处维护 |
| tokens.css/base.css | style.css | 变量和基础样式放在一个文件 |
| mocks 目录 | mock.ts | 演示阶段用一个文件集中管理 |

保留 MessageContent.vue，是因为 Markdown 和引用处理有明确的安全边界，并在多条消息中复用；保留 UploadDrawer.vue，是因为它由 App.vue 挂载，离开资料页后仍需查看上传状态。其余只用一次的布局和简单逻辑直接放在所属页面的 script/template/scoped style 中。

store.ts 保存跨页面数据并编排业务动作；页面筛选、弹窗开关等局部状态不全部塞进 store。文件内按 identity、chat、documents 分组即可，不为分组再建立接口、工厂或基类。业务查询、估算、发送、上传、重试、删除和改名均从 store 的操作入口发起；页面不得直接调用 api.ts 或维护业务请求控制器。

依赖方向固定：页面和展示组件 → store.ts → api.ts；上传操作由 store.ts → uploads.ts → api.ts。api.ts 不导入 store，由调用方显式传入 userId 和 AbortSignal；uploads.ts 不反向导入 store，状态变更通过创建队列时传入的回调同步，避免循环依赖。页面仅可从 api.ts 使用 import type 引用类型。

store 在操作入口捕获 userId、身份代次与 sessionId；身份代次在每次切换用户时递增，包括 A → B → A。晚到响应必须核对代次，不能只比较 userId。消息对账、删除后清理资料选择和查询失效集中在 store 处理；页面只传业务参数，不重复实现这些规则。

uploads.ts 在应用启动时由 store 创建一次队列实例，暴露响应式队列和操作函数。抽屉只展示状态、触发操作，不负责启动定时器。已知上传任务和恢复的 processing 文档使用同一轮询管理，禁止两个模块同时轮询同一文档。

这是一版起始目录，不是永久文件上限。只有出现跨页面真实复用、独立生命周期或明显难以维护的复杂逻辑时再拆；不因出现一个新接口、一个按钮或一个弹窗就增加文件。必要测试按业务流程组织，不为每个组件机械建立测试文件。

### 2.2 状态所有权

| 状态 | 保存位置 | 生命周期 |
| --- | --- | --- |
| 用户标识、模型偏好 | store.ts 的 identity 分组；开发标识可存 localStorage | 切换用户后清空全部业务缓存 |
| 会话与消息 | 后端权威，store.ts 的 chat 分组缓存 | 切换页面保留，刷新重新加载 |
| 当前草稿、模式、选中文档 | store.ts 中按用户与会话分组的内存状态 | 默认不把聊天正文持久化到浏览器 |
| 文件列表、状态 | 后端权威，store.ts 的 documents 分组缓存 | 上传、重试、删除后刷新 |
| 上传 File 对象和队列 | uploads.ts 的应用级队列实例 | 路由切换不丢失；刷新页面后不能自动恢复 File 对象 |
| 搜索词、状态筛选、分页 | URL 查询参数 | 分享/刷新当前资料视图可恢复 |
| 弹窗开关、行悬停 | 组件局部状态 | 随组件销毁 |

发送请求绑定发起时的用户和 sessionId。返回时写入对应会话，不能写到用户后来切换的页面。切换身份时中止本地请求、停止后续排队上传、清理引用和选中项，并忽略旧身份的晚到响应；已经进入后端的任务可能继续执行。

## 3. 当前可直接连接的接口

所有业务请求携带 `X-User-ID`。它是当前开发身份标识，不是账号认证；API Key、数据库地址和 MinerU Token 始终留在后端。

| 界面操作 | 方法与路径 | 现有响应/说明 |
| --- | --- | --- |
| 新建对话 | `POST /api/v2/sessions` | 201，`{title}` → 会话 id |
| 会话列表 | `GET /api/v2/sessions?offset=0&limit=50` | 数组，无 total、无搜索参数 |
| 读取消息 | `GET /api/v2/sessions/{id}?message_limit=50&message_offset=0` | 会话及消息数组 |
| 删除对话 | `DELETE /api/v2/sessions/{id}` | 204 |
| 发送问题 | `POST /api/v2/sessions/{id}/messages` | 非流式完整回答 |
| 上传单文件 | `POST /api/v1/documents` | multipart 的字段名是 `file`；202 表示已接受 |
| 文档列表 | `GET /api/v1/documents?offset=0&limit=50` | 数组，无 total、搜索或状态筛选 |
| 文档详情 | `GET /api/v1/documents/{id}` | status、chunk_count、error_message 等 |
| 处理详情 | `GET /api/v2/documents/{id}/processing` | 处理阶段，无准确百分比 |
| 重新处理 | `POST /api/v1/documents/{id}/retry` | 202 |
| 删除文档 | `DELETE /api/v1/documents/{id}` | 204；失败后可继续删除 |
| 健康状态 | `GET /health/live`、`GET /health/ready` | 不应以模型调用测试健康 |

代码依据：`app/api/routes.py`、`app/api/schemas.py`、`app/application/chat.py`。

### 3.1 问答请求映射

```json
{
  "question": "结合所选资料概括产品的核心目标",
  "search_mode": "auto",
  "model_provider": "deepseek",
  "document_ids": ["实际文档 UUID"]
}
```

- “自动”映射 `auto`；“知识库”映射 `knowledge`；“联网”映射 `web`。
- 全部资料：省略 `document_ids`，后端使用当前用户所有 ready 且向量模型匹配的文档。
- 指定资料：发送 1–100 个 UUID，仅允许选择后端返回 `can_query=true` 的资料；ready 是必要条件，还必须匹配当前向量模型。
- 选择器明确区分“全部资料”和“指定资料”。指定资料清空后禁止发送并提示选择资料，不偷偷切换成全库，也不发送 `[]`。
- 联网模式省略 `document_ids`，关闭资料选择；知识库模式省略 `web_query`。
- 自动/联网模式提供可展开的“公开搜索词”，映射 `web_query`，最长 400 字符；用于长问题或包含内部信息的提问。
- 问题最长 8000 字符。模型选项发送 `deepseek` 或 `ollama`；未指定时使用后端默认配置。

响应读取 `message_id`、`answer`、`citations`、`model_provider`、`model_name`、`token_usage` 和 `run_metadata`。展示实际响应模型，降级时以返回值为准。

### 3.2 消息加载、发送与失败

1. 新会话首次发送先创建会话，成功后再发消息；双击发送不得重复创建。
2. 页面添加临时用户气泡并显示“正在回答”。同一会话只能有一个本地发送任务。
3. 当前接口返回完整结果，回答与指标在请求结束后一起更新，不伪装逐字输出。
4. 成功后按服务器消息 ID 合并；必要时重新取最近消息对账，避免临时气泡重复。
5. 后端先保存 user 消息，失败时可能只留下问题。超时/断网后先读取会话，不自动重发 POST；当前没有消息幂等键，不能承诺安全的一键续跑。
6. 本地取消等待不代表后端终止推理；对话和上传都必须区分这两件事。
7. 历史接口选择最近 N 条后按时间正序返回。加载更早消息时 prepend 并按 ID 去重；分页期间有新消息时重新同步尾页。

会话列表初期使用“加载更多”；当前接口没有总数，不能直接显示图中那种精确总页数。

### 3.3 引用详情

知识库引用与网页引用按 `type` 分支渲染。点击消息中的 `[n]` 打开该消息对应的引用；引用编号仅在当前回答内有效。

- PDF 展示页码；PPT/PPTX 将 `page_number` 标为“第 N 张”；Excel 使用 section 显示工作表；Word 只展示实际返回的章节/页码。
- 当前只能展示 excerpt 与来源元数据，不能把箭头做成不存在的全文预览功能。
- 网页链接只允许 http/https，新窗口打开加 `noopener noreferrer`；不信任模型自行写出的 URL。
- 文档被删后，历史引用作为当时的片段快照展示，不能声称原文件仍可访问。

## 4. 文件与文件夹上传

### 4.1 复用方案

“上传文件”使用原生多文件选择；“上传文件夹”使用 `<input type="file" webkitdirectory multiple>`。浏览器返回目录及子目录内文件，`webkitRelativePath` 用于队列里区分同名文件。[MDN 文件夹选择说明](https://developer.mozilla.org/en-US/docs/Web/API/HTMLInputElement/webkitdirectory)。

前端遍历每个支持的文件，通过现有 `/api/v1/documents` 分别上传。无需先压 ZIP，无需给后端访问用户磁盘的权限。相对路径只作显示，真正上传的 filename 使用 `file.name`；后端继续使用 UUID 存储文件。

支持 `.pdf/.doc/.docx/.ppt/.pptx/.xls/.xlsx/.txt/.md`。不支持、空文件和超限文件在入队前列出原因，不静默丢弃。无支持文件时不发请求。不支持目录选择的浏览器退化为多文件选择。文件夹拖放可后续补充，首版以按钮选择目录为验收入口。

### 4.2 队列与状态

首版默认最多 2 个“正在传输或后台处理”的文件占用本地队列槽；202 后继续占槽，直到 ready/failed 或已确认删除才调度下一文件，避免文件夹一次触发大量后端进程内任务。用户要求停止剩余上传时，将未发送任务标为跳过并释放 File 引用，保留结果记录；已接受任务继续查询。网络故障或任务长时间未完成时暂停派发，仍可选择文件加入等待区。不能自动释放未知状态的槽位后无限积压。

该限制只约束当前页面实例，不是服务器全局并发限制；多标签页或多用户仍会增加后端负载。刷新后先恢复当前用户的 processing 文档并纳入槽位计数，完成核对前不派发新文件。

内部可以有 queued/transferring/processing/ready/failed；资料 UI 仍只使用三个主状态：

| 实际阶段 | UI 主状态 | 行为 |
| --- | --- | --- |
| 本地排队或传输中 | 上传中 | 传输阶段可显示真实字节百分比 |
| 后端 processing | 上传中 | 显示不定进度，说明“正在准备资料” |
| 后端 ready | 上传成功 | 是否可用于当前问答由 can_query 判断 |
| 后端 failed 或传输失败 | 上传失败 | 展开简短原因与重试 |

因此本产品“上传成功”的定义是文件已完成解析和向量入库，不承诺未来更换向量模型后依然兼容。202 和字节传输 100% 都不意味着可用于问答。无法计算解析百分比时禁止显示示例里的 68%。

删除属于操作状态，行显示“正在删除”并禁用选择；它不是第四种上传结果。已取消、格式不支持等放到本地批次结果说明中，不进入资料库主状态筛选。

抽屉底部计数使用互斥分类：发现总数 = 成功 + 失败 + 进行中 + 排队 + 跳过。只用已终结项/总数表示批次完成度，不能把传输字节百分比当作解析完成度。

### 4.3 轮询、重试与页面生命周期

- uploads.ts 的应用级队列在离开资料页后仍保留；关闭抽屉继续处理不等于关闭浏览器继续上传。
- 每个在处理的文档约 3 秒查询一次详情；上一请求完成后再排下一次，禁止重叠。长任务逐步放宽到 10 秒。
- ready/failed 后停止；页面隐藏降低查询频率，返回前台立即刷新。断网恢复后查询已知 ID。
- 刷新后从后端找回 processing 文档；尚未上传的 File 对象需要重新选择。切换用户清除原用户轮询。
- 已有 documentId 且后端 failed：调用 retry，不重新上传原文件；没有 ID 的传输失败：仍持有 File 时允许重新发送。
- `DUPLICATE_DOCUMENT` 显示“文件已存在”，不伪报新增成功。当前错误不带已存在文档 ID，不允许仅靠同名推断关联。
- 停止请求但未收到 202 时可能已经创建文档：标记结果待核对，刷新列表后由用户决定后续动作，不盲目重复上传。

### 4.4 长时间未完成与恢复

队列单独维护 `paused_reason`（network / long_running / unknown_submission / user）和每项的 `attention_reason`，不增加资料库的主状态类型。默认连续 3 次状态查询失败则暂停派发；浏览器明确离线时立即暂停。重连后先核对全部已接受任务，查询恢复成功再解除 network 暂停。

从收到 202 或重试成功起等待满 15 分钟仍未终结，标记“处理时间较长”并暂停派发；刷新恢复的任务第一次成功查询时开始本地观察。这个阈值仅用于交互提示，不宣告解析失败或自动终止远端任务。用户点击“继续等待”后重新开始 15 分钟观察，保留占用槽并解除该项的提醒；其他暂停原因仍需分别处理。

| 用户动作/查询结果 | 队列处理 | 后端行为 |
| --- | --- | --- |
| 刷新状态 | 立即核对已有 documentId | 只 GET，不重新提交 |
| 重试处理 | 暂停派发，针对同一个文档调用 retry | failed 或中断的 processing 可重试；正在处理时现有单进程锁返回 RESOURCE_BUSY |
| retry 返回 202 | 该项继续占原槽，重置观察计时 | 继续已有批次或按错误类型重新解析 |
| retry 返回 409 RESOURCE_BUSY | 保留任务与槽位，提示仍在处理 | 不重复调用、不强制重启 |
| 查询为 ready/failed | 清除该项提醒并释放槽 | 按真实终态显示，其他暂停原因清除后继续派发 |
| 查询/删除确认文档不存在 | 本地任务终结并释放槽 | 不再轮询，清理已选资料 |
| 从抽屉隐藏该项 | 只隐藏展示，继续跟踪且仍占槽 | 不视为取消、不触发替代上传 |

如果后端崩溃留下 processing，用户可通过上述 retry 恢复；SOURCE_FILE_MISSING 时提示删除记录后重新选择文件。若用户要取消已经接受的文档，走已有 DELETE，可能因仍在处理返回 409；失败时保留记录。

提交结果未知且没有 documentId 时，不能仅凭同名文件核对成功。保留待核对记录、暂停派发并允许刷新资料库；当前接口缺少上传幂等标识，不能承诺自动恢复。用户明确选择“重新尝试上传”时说明可能遇到已存在文件，并继续由后端内容去重保护。此操作不把原任务标为已取消。

## 5. 文件搜索、多选删除与列表契约

### 5.1 搜索需要后端支持

当前文档列表只有 offset/limit。只过滤已加载的一页会漏掉其他文件，不能满足“检索已上传文件”。最终采用服务端按文件名搜索、状态筛选和分页。

拟新增 `GET /api/v2/documents?q=&status=&offset=0&limit=50`，响应为：

```json
{
  "items": [],
  "total": 24,
  "offset": 0,
  "limit": 50
}
```

复用现有入库服务和仓储，不更改 `/api/v1/documents` 的数组响应。查询强制 user_id 过滤；q 最长 200 字符、按文件名大小写不敏感的字面子串匹配；通配符作为普通字符处理。status 接受现有后端枚举，UI 的三种状态做映射。服务端在全部匹配文件上分页，按 created_at/id 确定顺序。输入防抖约 300ms，筛选变动回第一页，过期响应不得覆盖新搜索。

表格大小列需要拟新增 `size_bytes: number | null`，上传时记录，旧数据未知显示“—”；不要用字符数或块数推算文件大小。文件相对目录首版只在当前上传抽屉显示；若要求刷新后仍显示路径，需要另加受校验的 relative_path 元数据，当前设计不承诺文件夹存储管理。

### 5.2 资料可用性契约（拟新增）

在共用 DocumentResponse 中增加以下字段，使现有 V1 上传/详情/重试响应与新增 V2 列表 items 保持一致；V1 列表仍返回数组。新增字段不得泄露路径或凭据。

| 字段 | 类型与约定 |
| --- | --- |
| size_bytes | 非负整数或 null；旧文件大小未知返回 null |
| can_query | boolean；后端按 status=ready 且 embedding_model 匹配当前配置计算，不作为数据库持久状态 |
| unavailable_reason | null 或 DOCUMENT_NOT_READY / EMBEDDING_MODEL_MISMATCH / DOCUMENT_DELETING |

can_query=true 时 unavailable_reason=null；否则必须给原因。判断顺序为 deleting、非 ready、模型不匹配。查询请求仍须在后端重新验证，can_query 只是响应时的快照，不能替代鉴权或范围检查。

资料库继续显示三种上传状态；对于 ready 但模型不兼容的文件，在选择器禁用，并提示“资料需要重新入库”。当前 retry 不接受 ready，不能对这类文件直接调用 retry；首版指引先保留原文件、删除旧记录后重传。若要无损迁移旧向量集合，应单独设计迁移流程。

阶段 B 对旧后端仅展示上传状态，并以“全部资料”范围跑通问答；can_query 字段缺失视为“可用性未确认”，不默认 true。完整指定资料选择器在阶段 C 接入新增字段后验收。上传恢复在阶段 B 可用分页扫描旧列表发现 processing 文档，阶段 C 改用服务端 status 筛选并遍历结果。

### 5.3 批量删除

复用单文档 DELETE，以最多 2 个并发逐项执行即可。选择范围限当前页，切换筛选或分页清空选择，避免误删不可见项。

用户点击删除后列出数量及文件名，确认后执行。204 删除成功；明确删除请求收到 404 可按目标已不存在处理；409 保留文件并提示稍后重试。部分失败保留失败项选择，成功项立即移除并从聊天资料范围中清理。不能因一项成功就提示“全部删除成功”。

## 6. Token 缓存命中率与上下文

### 6.1 当前缺口

`ModelRuntime` 目前汇总 input/output/total_tokens、attempts 和本轮最高输入 token 估算，并在每次模型调用前执行应用预算检查；尚未提供稳定的缓存命中统计契约和独立的上下文估算接口。会话 summary 字段存在，但没有自动压缩或手动压缩接口。

因此两项指标不能直接从图中数值或历史消息总字数得出，也不能把“本轮累计 input_tokens”当作上下文窗口占用。

### 6.2 缓存命中率口径

DeepSeek 文档提供 `prompt_cache_hit_tokens` 与 `prompt_cache_miss_tokens`。这反映提供方的输入缓存，与浏览器缓存、检索缓存和 MinerU 结果缓存无关。[DeepSeek 上下文缓存](https://api-docs.deepseek.com/guides/kv_cache/)。

拟由后端适配层从模型响应中归一化；安装的 LangChain 适配器究竟将这些值放在哪个 metadata 字段，实施时用实际响应与替身测试确认，不假设当前已透传。

```text
缓存命中率 = sum(hit_tokens) / sum(hit_tokens + miss_tokens)
```

分子分母只来自本轮已完整报告缓存统计的同一提供方/模型调用，包含回答修复调用。不能平均各调用百分比。降级后按提供方分别记录；Ollama 未提供同口径数据时显示“未提供”。零输入、统计缺失显示“—”，不是 0%。如果只有部分调用报告，标注“已报告调用”，显示 coverage，不声称整轮完整。

### 6.3 上下文占用口径

输入框旁固定展示“预计下轮上下文 / 应用输入预算”，并标明“估算”与所选模型；详情显示“本轮最高上下文占用率及对应调用”。两者都不是历史 token 累计账单。

- 预计下轮：系统提示、工具定义、摘要、实际选入历史及当前草稿的估算，加明确的工具证据预留。不是整个数据库历史长度。
- 本轮最高占用：每次调用分别计算 input_tokens / input_budget_tokens，再选择最高占用率对应的调用；输入长度、预算、provider 与 model_name 必须来自同一次调用。优先实际 usage，无法获取时标为估算，不累加多次调用，也不把不同模型的 token 数直接比较成统一峰值。
- 应用输入预算：后端按所选模型窗口、输出预留与应用限制计算；模型窗口应从部署配置获得，不能在前端写死 24k。
- 工具调用、回退与修复之前均重新计量并执行限制。仅在生成完后检查不足以防止超限。
- 估算器对 DeepSeek/Ollama 可能不是精确 tokenizer；现有 cl100k_base 只能作为近似值，并须处理编码资源离线可用性。
- 前端最多约 500ms 防抖请求估算，草稿作为 POST body，不能放 URL 或日志；旧响应按草稿版本丢弃。

默认产品提示阈值为 75%“建议压缩”、90%“优先压缩”；这是应用建议值，不是模型厂商规定。应用输入预算 = min(应用输入限制, 模型窗口 − 输出预留)，必须为正数；未配置可信模型窗口时不展示占用百分比，该模型不能启用预算保障功能。

组装每次调用时先按可用空间限制新增工具证据，保持工具调用/结果消息配对；仍然超限则返回 CONTEXT_BUDGET_EXCEEDED，不默默删除当前问题、不自动发起收费压缩。回退到另一模型和修复回答前重复此检查。估算误差仍可能导致上游拒绝，需保留明确错误提示；不能声称近似 tokenizer 实现精确窗口保障。

### 6.4 拟新增契约

以下均为拟新增契约，所有业务接口使用 X-User-ID，校验当前用户资源范围，拒绝未知请求字段；使用第 7.1 节统一错误结构。404 统一用于不存在或越权；422 用于参数不合法。新数值字段未知时使用 null，不能以 0 代替。

**能力查询：`GET /api/v2/capabilities` → 200。** 响应完整字段如下；示例预算不是前端常量：

```json
{
  "schema_version": 1,
  "default_model_provider": "deepseek",
  "models": [{
    "provider": "deepseek",
    "model_name": "deepseek-chat",
    "available": true,
    "input_budget_tokens": 24000,
    "cache_usage_supported": true
  }],
  "web_search_enabled": true,
  "upload": {
    "allowed_extensions": [".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".txt", ".md"],
    "max_file_bytes": 20971520,
    "long_running_warning_seconds": 900
  },
  "features": {
    "document_search": true,
    "document_queryability": true,
    "session_search": true,
    "session_rename": true,
    "metrics": true,
    "context_estimate": true,
    "context_compaction": true,
    "streaming": false
  }
}
```

available 只表示配置允许使用，不保证上游实时健康，不通过收费调用探测。models 枚举当前配置的提供方，预算未知为 null；默认提供方无可用配置时 default_model_provider=null。allowed_extensions 只返回当前真正可接受的格式，未启用 MinerU 时不包含 PDF/Office。功能尚未实现时返回 false；端点尚不存在的联调阶段仅启用已确认的基础接口。网络错误不能自动切换到 mock。

**上下文估算：`POST /api/v2/context-estimate` → 200。** 将 session_id 设为可选，支持新对话，无需先创建空会话：

```json
{
  "session_id": null,
  "question": "",
  "model_provider": "deepseek",
  "search_mode": "auto"
}
```

session_id 省略或 null 使用空历史；存在时必须属于当前用户的 V2 会话。question 允许 0–8000 字符，空字符串用于尚未输入的估算；model_provider 省略时使用默认模型；search_mode 默认 auto。可选 document_ids（1–100 个 UUID）与 web_query（1–400 字符）的互斥规则和范围验证与问答一致。此端点不创建消息、不触发收费生成或网络检索。

```json
{
  "session_id": null,
  "context_version": null,
  "provider": "deepseek",
  "model_name": "deepseek-chat",
  "estimated_input_tokens": 8200,
  "reserved_evidence_tokens": 6000,
  "input_budget_tokens": 24000,
  "usage_ratio": 0.341667,
  "measurement": "estimated",
  "estimate_basis": "cl100k_base_with_evidence_reserve",
  "thresholds": {"suggest": 0.75, "urgent": 0.9},
  "recommendation": "none"
}
```

estimated_input_tokens **已包含** reserved_evidence_tokens，前端不再次相加。reserved_evidence_tokens 依模式与实际可用工具计算，与执行时证据预算使用同一规则。usage_ratio 是估算总输入/预算，可大于 1；进度条可截到 100%，文本保留真实值。推荐值为 none / suggest_compaction / urgent_compaction / over_budget；超预算估算仍返回 200，以便展示。

预算未知时 input_budget_tokens、usage_ratio、recommendation=null，显示“预算未配置”；估算功能关闭返回 503 FEATURE_UNAVAILABLE，模型未配置返回 503 MODEL_UNAVAILABLE。文档未就绪或不兼容沿用现有 409 错误。

context_version 对已有会话必须返回不透明字符串，由最新消息 ID、summary 与 summarized_through 的规范化内容计算哈希，空会话也有版本；无 session_id 则为 null，不需要新增版本列。前端以“身份代次 + sessionId + 草稿序号 + 模型/模式/资料范围”核对响应，任何一项变化都废弃旧结果。发送或压缩后重新估算，发送端仍在调用前核对真实输入。

**运行指标：** 回答响应沿用 run_metadata，schema_version 升为 2；保留既有运行字段并新增 metrics。以下为新增部分示例：

```json
{
  "schema_version": 2,
  "metrics": {
    "cache": {
      "scope": "reported_calls",
      "by_model": [{
        "provider": "deepseek",
        "model_name": "deepseek-chat",
        "hit_tokens": 12800,
        "miss_tokens": 7200,
        "hit_rate": 0.64,
        "reported_calls": 2,
        "total_calls": 2,
        "complete": true
      }]
    },
    "context": {
      "calls": [{
        "attempt_index": 1,
        "provider": "deepseek",
        "model_name": "deepseek-chat",
        "input_tokens": 8000,
        "input_budget_tokens": 24000,
        "usage_ratio": 0.333333,
        "measurement": "reported"
      }, {
        "attempt_index": 2,
        "provider": "deepseek",
        "model_name": "deepseek-chat",
        "input_tokens": 12000,
        "input_budget_tokens": 24000,
        "usage_ratio": 0.5,
        "measurement": "reported"
      }],
      "highest_usage_attempt_index": 2,
      "complete": true
    }
  }
}
```

cache 始终使用 by_model 列表，即使只有一个模型也不改变结构。页面单值显示实际回答模型的已报告统计，详情列出其他模型；不得合成不同提供方的伪统一命中率。模型名来自实际响应，上例仅演示当前项目配置。历史消息缺少 metrics 时显示不可用。

每个被尝试的模型都必须出现在 by_model 中。total_calls 计入失败尝试；reported_calls 仅计完整报告 hit/miss 的调用。没有报告时 hit_tokens、miss_tokens、hit_rate=null；已报告总输入为零时 hit_rate=null。complete 表示所有尝试都已报告，部分报告只累加已知值；coverage 由 reported_calls/total_calls 展示。

context.calls 按真实调用次序记录，包括回退和修复；每项记录自己的模型与预算。measurement 为 reported / estimated / unavailable；无法获得输入数量时 input_tokens 和 usage_ratio=null。highest_usage_attempt_index 指向已知占用率最大的调用，平局取较早调用，无已知值为 null。complete 仅表示所有尝试都有输入数量和预算，估算仍须单独标注；不完整时只能称“已知调用中的最高占用”。

**会话搜索：** 扩展现有 `GET /api/v2/sessions` 的可选 q（0–200 字符，首尾去空格，标题字面子串匹配），保留数组响应和 offset/limit，按 updated_at/id 倒序；不新增总数接口，继续用加载更多。

**会话改名：`PATCH /api/v2/sessions/{id}` → 200。** 请求严格为 `{ "title": "新品规划" }`；去除首尾空白后 1–200 字符，返回完整 SessionResponse。更新 updated_at，忙碌返回 409 RESOURCE_BUSY；返回后由 store 更新列表与当前标题。

### 6.5 指标什么时候同步更新

V3.0 使用非流式问答：阶段 D 接通估算后，在输入时更新下轮估算；完整回答返回时同步更新本轮缓存统计与最高占用调用。阶段 B/C 没有估算能力时显示“暂不可用”。旧值在等待期间标注“上一轮”，新会话无历史指标则为“—”。模型切换后立即重算，不能将上一模型的值贴到新模型下。

生成过程中的实时统计作为后续增强，届时新增 `POST /api/v2/sessions/{id}/messages/stream`，使用 fetch 读取 SSE，因为请求需要 POST body 和 X-User-ID；当前 V1 的 stream 接口只返回 501，不能复用。V3.0 的“同步更新”指回答完成时同步显示统计，不等同于生成过程实时刷新。

后续首版 SSE 只包括 `run_started`、`context_usage`、`usage`、`done`、`error`，**不发送 answer_delta**。每个事件带 run_id、递增 sequence；done 在最终引用校验并持久化成功后发送，包含权威完整回答。缓存数据仅在上游报告时更新，不承诺逐 token 获得命中率。

当前 Agent 以 JSON 输出并可能修复，因此先流式发送状态和统计，最后发送校验后的完整答案；不能直接把工具调用或原始 JSON 当作回答流。逐字回答作为另一项增强，需先设计暂定回答与最终替换语义。断线不自动重放 POST，需后续 run 状态/幂等支持才可保证恢复；这些事件和恢复协议的详细设计不属于当前非流式接口契约。

### 6.6 上下文压缩

V3.0 阶段 D 包含手动“压缩上下文”，拟新增 `POST /api/v2/sessions/{id}/compact`，并同时修改 ChatService 读取历史的方法。能力未开放时只保留占用说明，不展示可点击的假操作。

请求为 `{ "expected_context_version": "估算接口返回的非空版本", "model_provider": "deepseek" }`。expected_context_version 必填；model_provider 可省略以使用默认模型。新对话未建立或版本未知时不提供压缩按钮，先查询估算。后端按同一口径估算决定是否有可压缩内容，默认保留最近 2 个完整问答及所有尚未回答的问题；更早的完整问答与已有摘要一起生成新的摘要。

由于目前没有 turn_id，首版只把相邻 user/assistant 视为可确认的完整问答；从上次摘要边界起，仅压缩连续的完整问答前缀，不能越过孤立的 user 或无法确定归属的消息推进 summarized_through。存在歧义时保留该消息及之后的原文；没有可安全推进的前缀返回 NOTHING_TO_COMPACT，避免摘要边界把未回答的问题跳掉。

成功响应 200：

```json
{
  "session_id": "00000000-0000-4000-8000-000000000001",
  "context_version": "new-opaque-context-version",
  "summarized_through": "00000000-0000-4000-8000-000000000002",
  "summary_updated": true
}
```

响应不返回杜撰的“节省比例”；store 收到成功后加载会话并按当前草稿重新估算。版本不符返回 409 CONTEXT_CHANGED，用户刷新后再决定是否重试；会话正忙返回 409 RESOURCE_BUSY；没有更早的可压缩完整问答返回 409 NOTHING_TO_COMPACT；模型错误返回 502/504，功能关闭为 503 FEATURE_UNAVAILABLE。所有失败均不更新摘要。

压缩必须基于服务器选定的已完成消息边界生成摘要，保留最近完整问答；成功时原子更新 summary 与 summarized_through。生成失败不改变旧摘要、不删除原消息。随后组装上下文时只读摘要边界之后的原始消息，防止摘要和旧消息重复进入模型。

生成摘要期间使用与问答相同的会话并发控制；提交前核对 context_version 和消息边界未变化，否则返回 409 CONTEXT_CHANGED。重复提交同一旧版本不会再次压缩；网络超时后先重新估算核对版本，不自动重发 POST。摘要只用于理解历史，不可作为引用证据。若上下文超长主要来自工具结果，应限制工具证据，不能反复压缩历史却不处理真正的超限来源。

压缩本身也受模型输入/输出预算约束；首次压缩的源历史过长时按完整问答划分有限批次，所有阶段成功后才提交一次摘要与边界。不允许为了压缩而发送另一条超预算请求，批次间保留问答配对；失败保留旧摘要及全部原消息。单次最多 3 个模型尝试（含降级），超出返回 409 COMPACTION_LIMIT_EXCEEDED 并提示新建会话，不删除或截掉未覆盖历史。

本轮仅设计该能力；在接口实现前不提供假压缩按钮。

### 6.7 契约验收与发布范围

V3.0 完成交付范围为阶段 A–D：两个页面、文件夹上传、搜索删除、资料可用性、非流式问答、随回答同步返回真实指标、下轮估算和手动压缩。SSE、逐字输出、断线续跑、正式认证及持久化任务队列为后续增强，不以占位实现计入完成。

| 契约 | 成功 | 关键失败与空值 |
| --- | --- | --- |
| capabilities | 200，第 6.4 节固定结构 | 不可用能力为 false；未配置预算为 null；上游健康不由此保证 |
| V2 文档列表 | 200，items/total/offset/limit | 无匹配为 items=[]、total=0；越过末页 items=[] 但 total 可大于 0；limit 1–100、offset≥0；非法筛选 422 |
| 文档可用性 | 文档响应新增 can_query/unavailable_reason/size_bytes | 原因必须与 can_query 一致；旧大小未知为 null |
| 会话列表搜索 | 200，保持现有数组结构 | 空结果为 []，无 total；q 最长 200；非法参数 422 |
| 会话改名 | 200，完整 SessionResponse | 404、409 RESOURCE_BUSY、422 |
| context-estimate | 200，第 6.4 节固定结构 | 无会话的版本为 null；409 文档不可用；503 功能/模型不可用；超预算仍 200 |
| compact | 200，新版本及摘要边界 | 404、409 版本冲突/正忙/无可压缩内容/批次超限、422、502/504、503 |
| 非流式消息 | 200，既有响应 + run_metadata schema_version=2 | 缺失指标为 null/空列表及完整性标志；409 CONTEXT_BUDGET_EXCEEDED；既有业务错误保持不变 |

文档查询排序固定 created_at/id 倒序，total 仅统计当前用户且匹配筛选的服务器记录。本地尚未发送的文件只出现在上传抽屉，不计入服务器 total。所有日期使用带时区 ISO 8601；比例用 0–1 浮点数（超预算可 >1），显示层再转百分比。

新契约定义仍放在现有 schemas.py、路由和相应应用模块中，前端类型集中在 api.ts。契约测试校验上述结构、null 语义与错误码；mock.ts 使用同一份类型与响应规则，不再建立一套演示专用协议。

## 7. 开发代理与部署接入

浏览器只向同源 `/api/...` 请求。开发期 Vue/Vite 在 5173，FastAPI 在 8000，通过 Vite proxy 转发 `/api` 与 `/health`，保留完整路径。该方式由 Vite 的 server.proxy 支持。[Vite 代理配置](https://vite.dev/config/server-options)。

拟采用的配置示意：

```ts
server: {
  host: '127.0.0.1',
  port: 5173,
  proxy: {
    '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    '/health': { target: 'http://127.0.0.1:8000', changeOrigin: true }
  }
}
```

部署时由同一站点提供构建后的静态文件，并将 `/api`、`/health` 转到 FastAPI；SPA 路由刷新回退到 index.html，但 API 404 必须保留 JSON，不能回退到 HTML。Vite 开发代理不会随构建进入生产。

当前 FastAPI 未配置 CORS。若后续必须采用不同源域名，需显式配置允许的前端 origin、X-User-ID/Content-Type 请求头、实际 HTTP 方法，以及暴露 X-Request-ID；不使用任意域通配替代配置。

长问答的浏览器/代理超时应大于后端 agent_timeout 并留响应余量。未来 SSE 要关闭代理缓冲。任何 `VITE_*` 都应视为公开信息，不能放服务端密钥。

### 7.1 API 客户端规则

- JSON 请求添加 Content-Type；multipart 由浏览器生成 boundary，不手工设置 Content-Type。
- 204 直接返回，不执行 response.json()。
- 错误统一为 `{code, message, requestId, status}`；优先解析后端 `error`，非 JSON 网关响应使用友好默认信息。
- GET 可做有限退避重试；创建、发送、上传不自动重试，避免重复记录或重复计费。
- 409 按 code 区分忙碌、重复文档、资料未就绪和范围变化；422 给出参数提示；413 指明超限；503 指明服务尚未就绪。
- UI 可复制 request_id 供排查，不展示堆栈、密钥或上游原始报错。
- 未来正式账号认证应由后端替换信任 X-User-ID 的机制，不能仅在前端做登录页就视为完成认证。

## 8. 后端改动清单与边界

| 优先级 | 改动 | 服务位置 | 原因 |
| --- | --- | --- | --- |
| 接入基础功能 | 无需改动现有问答与单文件接口 | api/routes.py | 可先跑通聊天和上传闭环 |
| 完整资料页必需 | V2 文档查询 q/status/total，共用响应增加 can_query/unavailable_reason | v2.py、schemas.py、ingestion.py、repository.py | 覆盖全部上传文件，避免选择不兼容资料 |
| 图中大小列 | 新增可空 size_bytes | 持久化模型、上传服务、迁移、响应模型 | 刷新后仍能显示大小 |
| 图中会话搜索/编辑 | V2 列表新增可选 q；新增 PATCH sessions/{id} 接受 title | 会话服务、仓储、路由 | 不把本地过滤或改标题伪装成持久功能 |
| 准确 UI 能力开关 | capabilities | 配置、路由 | 不暴露秘密，反映模型与上传限制 |
| 用量需求必需 | 缓存统计归一化、metrics 持久化 | providers.py、agent.py | 覆盖模型降级、修复和缺失用量 |
| 上下文提示必需 | 无会话也可用的 context-estimate 与逐模型调用预算执行 | chat.py、agent.py | 支持新对话，降级不混用模型预算 |
| 压缩操作 | compact、历史边界读取 | 会话/聊天服务 | 完整实现后才开放按钮 |
| 后续增强 | V2 SSE，之后再设计 run 状态和幂等恢复 | 新流式路由、工作流 | 单独验收生成中实时反馈，不纳入 V3.0 |

文件夹展开和批量删除优先由前端复用接口；不新增批量服务、任务管理平台或目录数据库。当前后台仍是单进程任务，首版沿用现有部署约束。

## 9. 实施顺序与验收

### 阶段 A：Vue 界面与演示数据

按三张图和第 2 节精简目录实现路由、布局、消息、引用、资料表和上传抽屉。演示数据与模拟行为统一放在 mock.ts，明确演示模式；api.ts 在演示模式下使用它，页面不直接依赖 mock.ts，mock.ts 对 api.ts 的类型引用仅使用 import type，避免运行时循环依赖。搜索、选择、删除确认和上传队列通过模拟结果演示；指标模拟值不能混入真实接口模式。视觉实现不直接把整张概念图当页面背景。

### 阶段 B：现有接口接通

连接用户标识、V2 会话与问答、V1 上传/重试/删除、真实状态轮询。实现目录展开、小并发队列与长时间未完成的恢复入口。测试“上传 → ready → 全部资料提问 → 看引用 → 删除”闭环；指定资料选择在阶段 C 接入可用性字段后验收。

### 阶段 C：补齐完整资料管理

增加服务端文件搜索、状态筛选、total、size_bytes、can_query/unavailable_reason、会话搜索与改名、capabilities。同步更新前端类型与后端契约测试。此阶段完成后启用总量分页、全部文件搜索和指定可用资料选择。

### 阶段 D：真实用量和上下文管理

完成缓存统计、支持空会话的估算、逐模型调用预算执行和手动压缩；所有指标从后端读取且注明完整性。通过非流式响应同步更新本轮统计后，完成 V3.0。SSE 与断线恢复另开后续增强阶段，不是本阶段的隐含工作。

### 验收重点

1. 所有页面不存在版本切换；对话使用 V2 会话，资料入口统一位于左侧菜单。
2. 文件夹包含嵌套目录、同名文件、不支持文件、超限文件时，每个文件都得到明确结果；失败不阻塞整个批次，停止排队不谎称取消后端任务。
3. 202/字节 100% 不能显示上传成功；选择器只允许 can_query=true。ready 但向量模型不匹配时禁用并说明原因。
4. 搜索命中未加载页的文件；筛选、分页与批量删除保持正确范围；部分删除失败能够单独重试。
5. knowledge 不发 web_query，web 不发 document_ids，指定资料清空不变成全库。
6. 问答超时、切换会话、切换用户、重复点击时不串消息、不自动重复收费请求。
7. 缓存统计覆盖命中、零命中、缺失、部分上报、降级与修复；上下文占用与累计消耗明确分开。
8. 压缩失败不丢历史，成功后摘要不与已覆盖原消息重复注入；所有新增接口仍验证用户所属范围。
9. 引用、Markdown、文件名中的 HTML/脚本按不可信文本处理；键盘操作和中文输入正常。
10. 新对话尚无 sessionId 时仍能估算，空草稿不产生会话；切换模型后重新估算，降级后的占用使用对应模型预算。
11. 模拟后端重启留下 processing、连续查询失败、已接受但未收到响应：队列不会无限加任务，有明确等待和恢复操作；隐藏抽屉项不释放未确认终结的槽位。
12. 页面无业务 HTTP 调用，A → B → A 身份切换不会接收上一代响应；mock 与真实接口遵守同一契约。
13. 通过 TypeScript 检查、构建、状态映射与接口契约测试，再在浏览器验证桌面和窄屏关键流程。

## 10. 本轮交付

V3.0 已创建 12 个前端源文件并接通 FastAPI，完成对话、资料管理、文件夹队列、引用、运行指标、上下文估算与手动压缩。后端新增接口和 `002_frontend_v3.sql` 迁移已落地；SSE、正式认证和多进程任务租约仍按本文边界留作后续增强。
