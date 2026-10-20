# 已有功能启用与 Worker 部署

完整配置包含独立入库、Run 与评测 Worker，以及版本索引发布和团队空间。先完成迁移、旧数据核验与预检，再启动新入口。各阶段实际验收和业务部署状态见[修复进度](replication-repair-progress.md)；功能启用不代表问答质量门禁已通过。

## 启用顺序

1. 停止旧 API/Worker 写入，备份数据库、`.env`、上传与解析目录。Milvus 数据保持原位，不重建或删除集合。
2. 执行 `python -m app.migrate`。已有库先核验基线，新增模块表、角色、任务类型与 Worker 心跳表。
3. 执行 `python -m app.bootstrap --model-center`，只初始化缺失模型配置，不覆盖已保存配置。
4. 在维护配置中先准备版本管理和 durable 入库。旧文档需要层级时，在绑定前使用同步补建命令完成；然后按 [旧索引核验与绑定](index-adoption.md) 逐份预检和提交。不要重建向量或以片段样本伪造绑定。旧 Run／评测没有具体索引快照时，应在原配置下完成或显式取消，不能自动改写检查点。
5. 在 API/Worker 的相同环境中开启 `MODEL_CENTER_ENABLED`、`SHARING_ENABLED`、`DOCUMENT_VERSIONS_ENABLED`、`DOCUMENT_PUBLICATION_ENABLED`、`WORKSPACES_ENABLED`、`CHAT_RUNS_ENABLED`、`HIERARCHY_ENABLED`、`KNOWLEDGE_QUALITY_ENABLED`，设置 `INGESTION_MODE=durable`。启用评测时还需 `EVALUATION_ENABLED=true` 和独立评测 Worker；规划和证据判断会增加受预算限制的模型调用。
6. 确认上传/解析目录存在，并运行 `python -m app.preflight --allow-missing-worker`。这里只允许 Worker 暂未启动，旧索引、源文件、冻结范围等失败项仍需处理。共享库没有当前版本会提示核对，但不会自动挑选历史版本。
7. 启动独立入库、Run、评测 Worker，再执行完整 `python -m app.preflight`；通过后启动 API/前端。缺失、过期或配置不匹配的 Worker 会导致预检失败，readiness 同样返回未就绪。

预检只读数据库、目录、源文件与向量结构；不创建文件、不执行迁移、不修改模型配置、不调用付费模型。版本发布开启时，核查所有就绪版本的绑定及源摘要，包括仍位于原集合/模型的版本。退休清理完成的墓碑不再要求源文件存在。已有未清理绑定时关闭发布开关会被拒绝，避免退回全局集合读取。`pass` 的模型检查只表示本地配置完整，不能代替真实推理验收。未有平台管理员时给出注册提示，保留普通账号权限。

Rerank 仅在提供合法 HTTPS endpoint、model 和 key 后启用；未配置时保留关闭并明确显示。平台管理员通过管理页“部署预检”可查看同一份检查结果。

## 独立 Worker

Worker 每 10 秒写一次数据库心跳，超过 45 秒视为不可用。API/Worker 必须匹配上传、解析目录、向量库和集合、embedding 模型/维度、分块配置及相关功能开关。Docker 中两者挂载同一个 app_data 卷并使用同样路径。

容器通过私有 `/tmp` 标记文件绑定自己的 Worker ID；healthcheck 检查该 ID 的有效心跳，不会因其他健康实例而把死亡实例标记为健康。优雅退出清除心跳，异常退出依靠超时失效。任务执行仍沿用已有租约、心跳、有限重试及文件锁。

## 旧文档补建

显式为当前模型下的一份 ready 文档入队：

```powershell
python -m app.hierarchy_backfill --user-id USER_UUID --document-id DOCUMENT_UUID --enqueue
```

任务使用现有持久入库队列，kind 为 hierarchy，重复提交正在排队/执行的补建不会重复入队。失败按已有有限重试策略处理，耗尽后可显式重新提交。补建读取已有 Milvus 叶子，不调用 embedding，不写向量；失败不会把原 ready 文档标为 failed，也不会清空原切片数量。

单文档同步补建命令仍保留，适用于维护窗口；日常补建使用 `--enqueue`。开启三级分块不会自动扫描并重建全部历史文档。

## Docker 完整配置

`compose.yaml` 默认将 API 和独立入库 Worker 同设为 durable；完整既有功能通过覆盖文件显式开启：

```bash
docker compose -f compose.yaml -f compose.full.yaml build
docker compose -f compose.yaml -f compose.full.yaml run --rm backend python -m app.migrate
docker compose -f compose.yaml -f compose.full.yaml run --rm backend python -m app.bootstrap --model-center
# 有历史文档时，在此先补建层级、执行 index_adoption；按其两步流程核验后绑定。
docker compose -f compose.yaml -f compose.full.yaml run --rm backend python -m app.preflight --allow-missing-worker
# 镜像已预置上传/解析目录；旧卷仍须核查实际权限和受控路径。
docker compose -f compose.yaml -f compose.full.yaml up -d ingestion-worker run-worker evaluation-worker
docker compose -f compose.yaml -f compose.full.yaml run --rm backend python -m app.preflight
docker compose -f compose.yaml -f compose.full.yaml up -d backend frontend
docker compose -f compose.yaml -f compose.full.yaml exec backend python -m app.preflight
```

已有向量集合不执行 `--milvus`；新环境另行显式初始化。迁移本地历史文件到容器时，还须受控迁移文档记录中的绝对路径；只复制数据库和源码不会自动完成文件迁移。预检会检查记录路径是否位于当前上传根目录且文件存在。

补齐第三阶段后，完整配置还会启动独立 `run-worker`。开启 `CHAT_RUNS_ENABLED` 时，readiness 和部署预检均要求配置匹配的 Run Worker；入库 Worker 的心跳不能替代它。运行/取消/人工恢复协议见[持久 Run 执行](durable-run-execution.md)。
