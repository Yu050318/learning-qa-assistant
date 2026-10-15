# 第二阶段：Excel 表格感知解析与行级引用

## 已完成范围

在 Supervisor 多 Agent 的已推送版本 `24069b7` 上，复用现有上传、文档状态机、Embedding、混合检索、EvidenceRegistry 和 Vue 引用面板，增加 XLSX 的行语义与来源贯通。
本阶段只实现 Excel 行级引用；父子块、精排与正式 A/B 评测仍是后续工作，不宣称检索准确率提升。

```text
上传 .xlsx → 校验容器 → LocalDocumentLoader → 本地 load_excel
  → 每行保留表头、字段值、单位、Sheet、物理行号与锚点
  → 长行拆片，每片重复第一列身份；不把不同行合并
  → Chunk.excel_source → Milvus 可空 JSON 字段
  → SearchHit → RAG Agent / Supervisor → EvidenceRegistry
  → 知识库引用 → 既有消息 JSON / SSE done / 历史接口
  → Vue 原引用面板：工作表 · 第 N 行
```

`page_number` 不用作 Excel 行号。来源由真实解析位置生成，模型不能自行提供 Sheet 或行号。
相同内容出现在不同物理行时不会被检索去重合并；无来源的旧文档保持原有去重规则。

## 表格规则

- 仅新上传的 `.xlsx` 走本地解析，不需要 MinerU，也不会上传到 MinerU；`.xls` 继续使用已配置的 MinerU。
- 首行是单层表头，不允许空缺、重复或超过 100 字符的表头；数据列必须有对应表头。
- 多 Sheet 分别处理，隐藏 Sheet 不进入索引；空行跳过但后续物理行号不变。
- 单元格绑定字段名，表头中的单位原样保留；日期使用 ISO、布尔值使用 true/false、空值显式标注。
- 不执行公式；仅读取文件内已保存的计算值。缺少缓存或缓存为错误值时拒绝，要求先计算并保存。外部链接不主动加载。
- 首版拒绝合并单元格及带宏内容。最多 32 张表、10000 行/表、100 列、总计 100000 单元格；同时受原文件大小、文档字符和分片数限制。
- 第一列作为行身份，最多 200 字符；长行拆片重复该身份，来源仍指向同一物理行。
- `xlsx-row-v1` 进入解析记录与确定性 Chunk ID，原有非 Excel Chunk ID 算法不变。

## 来源契约

知识库引用增加可选字段，旧引用无需修改：

```json
{
  "excel_source": {
    "sheet_name": "设备参数",
    "row_start": 4,
    "row_end": 4,
    "source_anchor": "sheet:%E8%AE%BE%E5%A4%87%E5%8F%82%E6%95%B0:row:4"
  }
}
```

当前每片只对应一行；类型允许行范围，以便后续父子块实现继续复用，但不会把多个离散命中伪装成连续范围。
检索时校验来源结构和锚点，不合法的向量元数据返回 `VECTOR_METADATA_INVALID`。

## Milvus 兼容与升级

新增集合自动包含 `excel_source` 可空 JSON 字段；既有固定字段集合继续支持旧文档的读写。
应用启动不会修改集合。旧集合上传新的行级 XLSX 前，显式执行：

```powershell
.venv/Scripts/python.exe -m app.bootstrap --excel-provenance
```

此命令只增加字段，不删除集合、不清空向量、不重新解析旧文档，重复执行可安全跳过。
已有行在新增字段上为空。保持原检索签名，因为 Embedding、Dense/BM25/RRF 和现有字段语义未改变。
旧集合缺少该字段时，新 Excel 向量写入返回 `VECTOR_PROVENANCE_REQUIRED`，避免静默丢掉来源；升级后可重试失败文档。

在线增加字段需要服务器支持相应能力，目标为 Milvus 2.6+；参见 [Milvus 官方字段升级说明](https://milvus.io/docs/v2.6.x/add-fields-to-an-existing-collection.md)。
不支持时命令报错，原集合保持不变。先升级隔离环境服务端或使用新集合验证，不自动替换已有集合。
本次没有连接用户现有 Milvus 或运行真实字段升级。

旧 XLSX 的 MinerU 解析结果继续按原路径读取，不推断或伪造物理行号；本阶段不自动重建旧文档。
PostgreSQL 沿用文档 parser_metadata 和消息 citations JSON，不新增表、不修改已有表。

## 学习与验收

先读 `app/infrastructure/loaders/excel.py`，再看 `LocalDocumentLoader.chunks` 的“Excel 行不再通用切片”分支，最后沿 `Chunk.excel_source` 跟到 Milvus、EvidenceRegistry 和前端。
无需为此重写 Supervisor、SQL Agent 或 Web Agent。

```powershell
.venv/Scripts/python.exe -m pytest tests/test_excel.py -q
.venv/Scripts/python.exe -m pytest -q
cd frontend
npm run test:agents
npm run build
```

测试包含字段类型、空行、多 Sheet、隐藏 Sheet、长行、无缓存公式、合并单元格、资源预算、稳定 ID、元数据往返与旧集合兼容。
贯通测试使用内存 SQLite、向量替身和脚本模型，执行真实上传 → 入库 → Supervisor/RAG → 回答落库 → 历史引用读取，并验证用户隔离。
真实 Milvus JSON 往返测试放在既有 opt-in 存储集成测试中；隔离服务未配置时跳过。离线成功不代表真实集合升级已验证。

2026-10-15 验证：后端全量 **111 passed、8 skipped**，其中 Excel/来源测试 31 项；前端 4 项 Node 测试、TypeScript 检查和 Vite 构建通过；锁文件与 diff 空白检查通过。
浏览器以本地真实 XLSX 解析结果、脚本模型和离线服务替身验证回答、点击引用、Sheet/第 4 行展示与刷新历史；测试表保留第 3 行空白，确认物理行号不会重排。截图见 `output/excel-row-citation-smoke.png`。
实际 Milvus 字段升级及真实服务器上的 JSON 往返未执行，不宣称在线效果或准确率提升。
