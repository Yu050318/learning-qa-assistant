# 第三阶段：小块检索与父块上下文

## 本阶段的选择

在已推送的 Excel 阶段 `719abbb` 上，保留现有小块、Chunk ID、向量和 Dense＋BM25/RRF 排序。先召回小块，再读取同一分组的相邻小块作为父块上下文。
这是可复用现有索引的两层实现，尚未复刻参考项目完整的 L1/L2/L3 预分块和 Auto-merging 阈值策略，不把邻居读取包装成三级自动合并。
父块不单独向量化、不新增数据库表或集合字段，也不要求旧文档重新入库。

```text
RetrieverService
  → 原 Hybrid Search → 用户/文档校验 → 去重 → Top K 命中叶子
  → Milvus expand_context：按 chunk_index 每 4 块读取相邻分组
     → 同用户、同文档的查询过滤与结果二次校验
     → 按文件名、已有页码/章节、Excel Sheet 再隔离
     → 确认命中叶子仍存在且内容/元数据相同
  → SearchHit 保留原 chunk/score，附带可选 ParentContext
  → EvidenceRegistry：叶子编号；父块作为无编号 context_only
  → RAG Agent / Supervisor：理解上下文、引用命中叶子
  → 原 SSE / 消息 / 引用卡：仍指向原始片段及 Excel 物理行
```

父块只辅助理解上下文；未召回的相邻行不会自动获得引用编号。需要用到那一行的事实时，应再次检索取得对应证据。
编号合法、来源定位准确不等于已经验证事实蕴涵；本阶段没有新增答案语义审计，也没有声称答案准确率提升。

## 配置与兼容

```dotenv
RETRIEVAL_PARENT_CONTEXT=true
RETRIEVAL_PARENT_GROUP_SIZE=4
```

默认开启，分组大小允许 2～8。设置 `RETRIEVAL_PARENT_CONTEXT=false` 可回到原叶子检索，调用入口和模型配置不变。
组起点为 `chunk_index // group_size * group_size`，不是按物理行号猜测相邻行；长行有多个片段时，每片仍有独立 Chunk ID。
跨页、跨章节和跨 Sheet 的候选会从父块中排除；没有 Sheet/section 的旧 Excel 不构造父块，并记录 `PARENT_CONTEXT_BOUNDARY_UNKNOWN`。
页码不存在的文本文件按已知文档/章节边界处理，不凭空推断分页。

这一阶段不需要运行 schema 升级命令。上阶段新 XLSX 的 `excel_source` 字段仍按原要求显式升级；旧文本集合可以直接使用上下文扩展。
新功能没有改动用户现有 Milvus 数据，也没有重新调用 Embedding。

## 边界与失败语义

- 每组最多读取 `group_size + 1` 条，以发现重复索引；返回越权文档、用户、组外位置或重复位置时直接失败，不用“保留叶子”掩盖数据隔离错误。
- ParentContext 保留真实子块，标识由子块身份、正文和元数据确定性生成。父块必须包含本轮命中叶子的完整身份、正文与来源；检索层和证据层分别校验。
- 检索证据的数量、顺序、内容和分数都不能被扩展器改变；扩展不参与新的召回排序。
- 同一次扩展内，同组查询复用结果；缓存仅在该次调用内存在。新请求不会复用旧父块，内容发生变化会生成不同标识。
- 所有组查询共享一次 `MILVUS_TIMEOUT` 时间预算；每次查询使用剩余时间。连接和 schema 检查仍受原 Milvus 适配器超时约束，全局 Agent deadline 在工具调用后继续校验。
- 初始召回失败仍是服务故障。仅补上下文失败时保留健康叶子，显式记录 `MILVUS_UNAVAILABLE` 或 `PARENT_CONTEXT_TIMEOUT`。
- 命中叶子已变更/消失、父块正文超过 12000 字符、旧 Excel 边界未知时，保留叶子并分别记录 `PARENT_CONTEXT_STALE / PARENT_CONTEXT_TOO_LARGE / PARENT_CONTEXT_BOUNDARY_UNKNOWN`。
- EvidenceRegistry 优先登记本次命中叶子，父块最多占总证据预算的三分之一。预算不足时省略父块，记录 `PARENT_CONTEXT_BUDGET_EXCEEDED`；不截断后冒充完整父块。

## 工具结果与引用

知识库工具和子 Agent 返回值新增 `contexts`、`warnings`：

```json
{
  "sources": [{"number": 1, "type": "knowledge", "content": "实际命中的片段"}],
  "contexts": [{
    "context_id": "确定性 UUID",
    "type": "context_only",
    "for_numbers": [1],
    "title": "设备参数.xlsx",
    "content": "同一 Sheet 中的相邻片段，只作理解上下文"
  }],
  "warnings": []
}
```

contexts 没有 `number`，不会进入引用列表。重复父块只占一次预算，`for_numbers` 可以关联多个实际命中来源。
父块会传到 RAG Agent、Supervisor 和最终输出修复步骤，三个位置均约束它不能替代编号叶子证明事实。
`run_metadata.parent_context_count`、知识库工具事件和顶层 warnings 记录实际扩展及降级；不会将整段父块正文放进运行元数据或公开引用中。
引用类型和前端来源卡无需变更，Excel 仍显示真实命中的 Sheet/物理行，父块不会把一行引用扩大为整组范围。

## 验证与学习入口

先读 `app/application/retriever.py` 的扩展调用，再看 Milvus `expand_context` 和 `ParentContext`，最后看 EvidenceRegistry 的叶子与父块预算处理。
配置装配在既有 Services；没有第二套 RAG、SSE 或缓存服务。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_parent_context.py -q
.venv/Scripts/python.exe -m pytest -q
```

专项测试覆盖分组缓存、原排序保留、跨用户/文档拒绝、页/章节/Sheet 隔离、旧 Excel、过期叶子、正文与预算上限、超时、可见降级、请求间隔离，以及真实 LangChain Supervisor 接收上下文后仍引用原行。
真实 Milvus 往返放入原 opt-in 存储集成测试；当前缺少隔离服务配置，因此未执行，不用替身测试冒充在线验证。

已有 `scripts/evaluate_retrieval.py` 继续调用正式 RetrieverService，新增逐题父块数量与警告输出；Recall/MRR 仍按原始叶子 ID 计算。
后续应在固定数据集上测量答案质量、上下文成本和耗时，再决定是否加入三级持久父块与 Auto-merging；本阶段不填虚构提升指标。可选精排已在[第四阶段](rerank-retrieval.md)接入，默认关闭。

2026-10-15 验证记录：后端全量 **137 passed、9 skipped**，其中本阶段专项测试 26 项；`uv lock --check` 与 diff 空白检查通过。未修改前端组件、未运行真实 Milvus 邻居读取或在线模型对比，也未执行集合升级或重新 Embedding。
