# 固定合成企业语料与标注集

数据来自本地参考项目 SuperMew `eb0495e` 的 `evals/rag/synthetic_enterprise`，源资产声明为 **CC0-1.0**，全部是虚构的 Nebula Industrial 业务数据，不含真实企业资料。固定 seed 为 `20261010`。`upstream-manifest.json`、`upstream-dataset.json`、`source_records.json`、`ground_truth.jsonl` 和 `splits.json` 保留原始事实、标签、锚点与身份；`dataset.json` 仅适配本项目的评测契约，未改写问题、标准事实和开发/冻结分组。

## 上传边界

只允许使用本目录 `manifest.json` 的 `ingestion_allowlist`：`upload_corpus/` 中 **4 XLSX + 8 HTML，共 12 个业务文件**。产品参数、价格和库存各 200 行，维修记录 100 行。其余文件均属于评测控制数据，**禁止递归上传、索引题目、参考答案或标签**。

Gold Anchor 使用文件名、Sheet、物理行及章节；这些身份不是数据库 Document/Chunk ID。真正执行评测时，必须从当前用户授权的已入库文档和实际检索结果建立映射，不能预造 UUID 或将参考答案作为检索证据。

## 固定分组

80 题分为简单事实 20、表格查询 20、跨文档 20、无答案 10、版本/政策 10。20 题为开发集，60 题为冻结集；不得根据冻结题逐题调参。70 题预期有证据回答，10 题预期证据不足，均保留原标注。资产导入不是入库操作，也没有生成模型答案或质量提升报告。

```powershell
.venv/Scripts/python.exe scripts/verify_enterprise_fixture.py
.venv/Scripts/python.exe -m pytest tests/test_evaluation_dataset.py -q
```

验证器校验资产 SHA256、数据集指纹、固定分组和唯一上传清单；测试还核对真实 Excel Sheet/物理行，拒绝标签冒用用户、指定私有文档 ID 或请求联网。`manifest.json` 记录冻结资产的哈希，不允许在已有 v1 上覆盖标签；需要修改时创建新版本及新指纹。

工作台及独立 Evaluation Worker 的部署与使用见[操作说明](../../../docs/evaluation-workbench.md)。阶段验收状态见[修复进度](../../../docs/replication-repair-progress.md)；资产存在或脚本样例通过，均不能作为真实 A/B 质量结论。
