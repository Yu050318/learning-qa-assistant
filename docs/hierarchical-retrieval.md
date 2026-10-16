# 第十二阶段：三级分块与自动合并

本阶段补齐根块、父块、叶子三级结构。继续使用已有 LangChain splitter、PostgreSQL、Milvus、RetrieverService 和 EvidenceRegistry，没有增加框架或依赖。默认关闭，不自动修改已有数据库或重新入库。

## 参考与复用

参考本地 SuperMew 的 `backend/indexing/document_loader.py` 三级递归切分、`backend/indexing/parent_chunk_store.py` PostgreSQL 父块存储思路。Demo 沿用 token 切片配置，根/父/叶大小为 `CHUNK_SIZE × 4 / 2 / 1`，默认 2800/1400/700 token；根、父不重叠，叶子沿用 `CHUNK_OVERLAP`。尺寸沿用本项目现有 token 单位，并非逐参数复制参考实现。

新增 `app/infrastructure/loaders/hierarchy.py`，每个规范化页或章节独立递归，重复标题的不同解析单元也不会在新文本入库时合并。Excel 不二次切行，沿用现有真实物理行与长行片段，在同一 Sheet 内自底向上分组。每层至多 8 个直接子节点，每棵树至多 64 个叶子；节点正文上限 48000 字符，叶子总量沿用 `MAX_DOCUMENT_CHUNKS`。

`Chunk` 与 Milvus schema 不增加层级字段：叶子继续复用已有 embedding、混合召回、精排和权限校验。新增 `rag.hierarchy_nodes` 表保存三级节点、来源、正文 SHA-256 和配置指纹。叶子正文在 PostgreSQL 中另存一份，用于精确比对与层级重建，实际召回仍由 Milvus 完成。父子复合外键限制相同文档和所有者，节点随文档删除级联清理。

`IngestionService.process` 在向量全部写入后，将节点、层级 manifest 与文档 `ready` 状态放在最终同一事务提交。文档处理失败或 generation 已变化时不发布新树。原有 Worker 领取、租约 guard、文件校验和版本激活边界继续生效。

## 查询与证据

调用链：`Supervisor.search_knowledge → RetrieverService → 混合召回/精排 Top K → HierarchyService.expand → EvidenceRegistry → Supervisor 汇总`。

仅根据这次检索返回的真实叶子计算覆盖率：某父块的已命中叶子数 / 总叶子数达到 `HIERARCHY_MERGE_THRESHOLD`（默认 0.6），父块才可合并；某根块的达标父块数 / 总父块数达到同一阈值，才可上提根块。不添加新的召回证据，不改变原始排序、RRF 或 rerank 分数。

优先选择达标根块；根块超过传入预算时尝试父块，再不足则只保留叶子。同一上下文在一次扩展内只计费一次。跨文档所有者分组顺序调用，递减共享的上下文 token 预算；最终由 EvidenceRegistry 严格限制上下文不超过证据总预算的三分之一，并优先登记实际命中叶子。

查询按 owner/document/节点 UUID 限定，最多读取命中根块的有界子树。每次 SQL 查询按请求剩余 deadline 设置事务内 statement_timeout。校验文档状态、embedding 模型、源文件哈希、generation、manifest 指纹、节点正文哈希、直接/后代数量、边的层级及来源边界。缺失子节点不能降低分母或抬高命中比例；结构、归属或正文不一致直接拒绝。文档 manifest 过期或命中节点缺失则保留原叶子并标记 `HIERARCHY_STALE`。SQL 连接/查询故障标记 `HIERARCHY_UNAVAILABLE / HIERARCHY_TIMEOUT`，健康叶子仍可回答。

根、父块仅作为无编号 `context_only` 传给子 Agent 和 Supervisor。最终引用仍绑定原叶子 ID、页码/章节/Excel 行号；未命中的子块不能因为出现在父级上下文中获得引用编号。沿用前端来源卡和历史引用结构。`run_metadata.hierarchy_events` 记录实际选择层级、节点 ID、命中/总子数和合并/预算/兼容/过期状态，不存父级正文；原有 `parent_context_count` 和 warnings 继续有效。

没有 manifest 的旧文档继续使用原两层扩展（受 `RETRIEVAL_PARENT_CONTEXT` 控制）。关闭 `HIERARCHY_ENABLED` 后全量恢复原兼容路径，已保存的新树保留在数据库中。

## 显式启用和补建

先确认服务连接到期望的数据库，再手动执行：

```powershell
.venv/Scripts/python.exe -m app.bootstrap --hierarchy
```

在服务端配置 `HIERARCHY_ENABLED=true`，按需设置 `HIERARCHY_MERGE_THRESHOLD=0.6`（范围 0.5–1），同步重启 API 与 Worker。此后新入库使用三级结构。表初始化是增量创建，不会处理已有文档或向量。

旧文档按明确所有者和文档逐个补建：

```powershell
.venv/Scripts/python.exe -m app.hierarchy_backfill --user-id <所有者UUID> --document-id <文档UUID>
```

补建要求同一 embedding 模型下的 ready 文档，使用入库 advisory lock 和文档行锁，分页只读 Milvus 全部叶子，核验数量、连续序号、唯一 ID、所有者与文件名，在 PostgreSQL 中自底向上生成父、根结构，并原子替换该文档的旧树。命令不调用 embedding，不写/删 Milvus，不改变文档状态、文件、叶子 ID 或叶子正文；可重复执行。

补建保留旧叶子边界，不能让旧切片获得新的递归边界；manifest.mode=`backfilled-leaves` 与新文本的 `recursive` 明确区分。缺少 Sheet/section 的旧 Excel 拒绝补建，避免猜测来源。若需要新的切片尺寸，显式上传新版本，不用补建命令重新切片。

## 验证与演示

```powershell
.venv/Scripts/python.exe -m pytest tests/test_hierarchy.py tests/test_parent_context.py -q
.venv/Scripts/python.exe -m pytest -q
```

离线测试不调用付费服务，覆盖三级确定性结构、页/Sheet 边界、自动合并与预算回退、伪造/缺失节点、Supervisor 最终叶子引用、入库失败、补建只读分页及跨用户拒绝。本阶段最终全量结果为 332 passed、15 skipped、38 subtests passed，三级专项 19 passed。离线 splitter 使用字符替身，token 预算使用原保守离线估算；测试不代表真实模型回答质量。

`tests/integration/test_hierarchy.py` 提供真实 PostgreSQL 的显式验证，只接受库名含 `test` 的独立数据库，需 `RUN_HIERARCHY_INTEGRATION=1` 与 `HIERARCHY_TEST_DATABASE_URL`。本阶段使用独立、无持久卷、内存临时 PostgreSQL 18，已验证原子可见性、真实外键、根块合并、幂等替换和级联删除（1 passed）；测试完成即停止并删除临时容器。未升级现有平台库，未调用在线模型。真实 Milvus 与完整在线问答验证留在最终联调阶段，结果届时单独记录。

演示可上传包含完整参数段落的测试 Markdown，然后询问“同一设备的功率、输入电压和工作温度分别是什么？”。观察 `hierarchy_events`：集中命中同一父块会补父级，分散且不足阈值只保留叶子；命中多父块且达到根阈值时可补根级。最终来源卡仍只显示实际命中的编号片段。实际是否合并取决于召回分布与证据预算，不能仅凭问题文案保证上提。
