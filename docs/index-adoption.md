# 旧索引显式核验与绑定

适用于迁移后尚无 `document_indexes` 绑定的旧 `ready` 版本。先备份 PostgreSQL、原文件、解析缓存与部署配置，执行 `python -m app.migrate`；功能开关不负责创建表或自动认领索引。当前业务部署状态见 [功能补齐进度](replication-repair-progress.md)。

## 核验方式

`python -m app.index_adoption` 默认只读，不调用 Embedding、MinerU 或回答模型，不修改 Milvus、原文件、片段 ID、历史发布指针或旧检查点。

- 读取完整源文件并核对 SHA-256。本地文本、HTML、Excel 等复用本地解析器；原 MinerU 文档必须具备源摘要及文档类型匹配的完整 `normalized.json` 缓存，不以片段样本代替缓存，也不重新调用云解析。
- 按原分块参数重建完整预期内容。原递归层级使用递归分块；`backfilled-leaves` 继续按原平面叶子核验，不把补建父节点误当成重新递归切分。
- 从指定的原集合读回所有片段，校验模型、维度、所有者、文档、连续序号、实际总数及越界片段。逐项比较文本与来源边界；保留原片段 ID，再计算包含原 ID 的完整摘要。已有层级还需与对应 SQL 叶子及清单一致。
- 内容、参数、来源或数量不同即拒绝绑定；缺少可验证证据时，应恢复原缓存/参数，或建立新的候选版本。不能用当前默认模型或分块参数猜测旧索引身份。

## 两步执行

以下命令中的 UUID 需要替换为目标文档的实际所有者与文档编号。若当前默认配置已更换，追加 `--collection`、`--embedding-model`、`--embedding-dimension`、`--chunk-size`、`--chunk-overlap`，使用已核对的原配置。

```powershell
$inspection = .venv/Scripts/python.exe -m app.index_adoption --user-id "USER_UUID" --document-id "DOCUMENT_UUID" | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "索引核验失败，请检查稳定错误码" }
$inspection
```

确认完整摘要后，在维护状态中提交：停止 API 与各 Worker，完成或取消未结束的 Run／评测，避免旧进程继续创建旧格式快照。工具会拒绝仍有近期 Worker 心跳、活动 Run 或该文档待执行任务的写入，不会替用户修改这些执行记录。

```powershell
.venv/Scripts/python.exe -m app.index_adoption --user-id "USER_UUID" --document-id "DOCUMENT_UUID" --apply --maintenance --expected-leaf-sha256 $inspection.leaf_sha256
```

第二次执行会重新读取原文件和索引，摘要变化返回冲突。成功只持久化原索引绑定及核验来源；原当前版本仍为当前，历史版本保留为历史可检索版本，不自动切换个人或共享库指针。无版本记录的旧虚拟 V1 仅在提交时建立逻辑版本关系。重复执行已绑定版本会核对原摘要并返回 `already_bound`，不重新发布候选或覆盖绑定。

启用 `DOCUMENT_PUBLICATION_ENABLED` 和团队空间前，再执行部署预检。预检会检查全部就绪版本的绑定、源文件摘要、原绑定集合的可用性和未结束的冻结范围。旧 Run／评测缺少具体索引身份时，需要在原配置中结束或显式取消；不能用当前配置改写其快照。共享库存在多个历史版本但无当前版本时会提示核对，保留无当前版本也是有效选择，不自动选版。

## 验证边界

离线专项覆盖完整内容、稀疏缓存、源文件改动、原片段 ID 保留、两种层级模式、配置冲突、维护门禁及历史指针保留。显式存储集成在独立 PostgreSQL 与独立 Milvus 数据库运行 CLI 只读预检和绑定，检查向量值与原文件逐项保持；另验证后段内容变化、实际行模型不符及额外片段被拒绝。测试向量为固定合成值，未调用收费模型，不能据此声称问答质量提高。

`bootstrap --document-versions`、`--ingestion-jobs`、`--chat-runs`、`--sharing`、`--hierarchy` 保留为兼容入口，均执行同一完整平台迁移链，避免已有表缺列却报告初始化成功。正式部署建议直接使用 `python -m app.migrate`。
