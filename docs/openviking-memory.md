# OpenViking 个人自动记忆

当前交付包括独立 HTTP 适配、用户同意设置、持久同步队列、召回、本地删除屏障与 Vue 管理入口。默认关闭；启用服务器配置后，每名用户仍须在“长期记忆”页面主动开启。原有手工记忆、Milvus 知识库与 HTTP/SSE 接口继续工作。

## 数据流与删除语义

1. 只在个人聊天成功保存助手消息的同一事务内登记 capture。只发送该轮用户自己输入的文本；不回溯开启前的消息，不采集团队、评测、工具输出、模型回答。
2. `python -m app.memory_worker` 异步提交并轮询任务。OpenViking 的 commit 返回不代表提取完成，只有任务完成且差异通过归属验证，才发布本地可召回内容。[官方异步任务说明](https://docs.openviking.ai/en/api/17-tasks)
3. 手工与自动记忆按排名融合，使用原有总 Top K。自动召回超时返回降级代码；原问答继续，缺失的记忆不会伪装成成功记住。
4. 后端认证 UUID 与 generation 构成 OpenViking 的可信用户身份。客户端不能指定命名空间。仅接受自己的 profile/preferences/entities URI；不启用任务经验学习。
5. 单条删除立即清空本地正文并留下墓碑。关闭/清空轮换 generation，原空间立即失效；关闭会清空自动记忆，手工记忆保留。
6. 外部清理先等待此前远程提取到达终态，再删除来源会话和目标记忆。只删除会话不会自动移除已经提取的记忆，因此两者分别处理。[官方删除会话说明](https://docs.openviking.ai/en/api/05-sessions#delete-session)

`pending_jobs` 表示等待同步或外部清理；`failed_jobs` 包含失败及不确定结果。网络写入停在 appending/committing 时不盲目重放。缺少远程提交回执时，清空任务会标为 `MEMORY_CLEANUP_UNCERTAIN`，不会声称外部删除已完成。运维人员需要用相同身份核查远程任务、等待终态并清理对应 `rag-{job_id}` 会话与旧 generation 的记忆，再根据审计记录人工恢复清理任务；不要删除本地墓碑来“修复”。

删除某个文件后，未来提取到同一 URI 的内容仍被本地墓碑拦截；用户清空并主动重新提供信息后才进入新 generation。这是一项保守的首期策略。原应用中的聊天历史并非此 API 的删除目标，用户仍通过会话功能管理原聊天。

来源消息会复制到独立 OpenViking 服务，且可能被其配置的模型/Embedding 供应商处理。不要将生产会话发送给未审查的服务。服务仅在受信任后端网络可达；网关根密钥不能出现在浏览器、日志或 Git 中。此部署采用官方 trusted 模式，由后端断言 account/user/role。[官方多租户说明](https://docs.openviking.ai/en/concepts/11-multi-tenant)

## 配置与启动

镜像固定为 `ghcr.io/volcengine/openviking:v0.5.2.1`，digest `sha256:7c26f9777c7632fd4b771be77471634bffb6ec8d6338b39f53deb2da1b1d34b3`。版本升级须复核 API、存储兼容与删除语义。不要假设该版本会自动过滤所有密钥或私人信息。

配置模板为 `docker/openviking/ov.conf.example`。手动将副本放到被 Git 忽略的 `data/openviking/ov.conf`，填写独立网关密钥和已核对的模型/Embedding 配置。`root_api_key` 必须与应用 `OPENVIKING_API_KEY` 相同，`default_account` 与 `OPENVIKING_ACCOUNT` 相同。模板中的模型名和供应商地域必须与你实际账号支持的配置一致；示例不保证上游可用。

先备份并在目标环境显式执行：

```powershell
.venv/Scripts/python.exe -m app.migrate
```

本机已有独立 OpenViking 时，在 `.env` 中补充以下配置，然后重启 API、Run Worker 与独立 Memory Worker：

```dotenv
OPENVIKING_ENABLED=true
OPENVIKING_URL=http://127.0.0.1:1933
OPENVIKING_API_KEY=<private-gateway-key>
OPENVIKING_ACCOUNT=learning-qa
OPENVIKING_RECALL_TIMEOUT=1
OPENVIKING_WORKER_TIMEOUT=15
OPENVIKING_TASK_TIMEOUT=600
OPENVIKING_POLL_SECONDS=2
```

```powershell
.venv/Scripts/python.exe -m app.memory_worker
```

已有完整 Compose 部署可使用显式叠加配置；该生产叠加部署尚未启动，真实评测使用独立的测试容器和数据库：

```powershell
docker compose -f compose.yaml -f compose.full.yaml -f compose.openviking.yaml config --quiet
# 在迁移完成、配置及数据发送范围确认后执行：
docker compose -f compose.yaml -f compose.full.yaml -f compose.openviking.yaml up -d
```

OpenViking 端口仅向 Compose 内网暴露，应用通过 `http://openviking:1933` 连接。Memory Worker 是独立进程，API 不执行模型提取。服务端口不应向公网发布。所有 API/Worker 必须使用一致的数据库、feature flags 与网关配置。纯宿主机 Python 流程则使用宿主机可访问的 URL。

## 新增接口

| 方法 | 路径（`/api/v2` 下） | 结果 |
| --- | --- | --- |
| GET / PATCH | `/memory-settings` | 可用性、用户开关、代际、待处理及失败数 |
| GET | `/automatic-memories` | 仅当前用户当前代际的已完成自动记忆 |
| DELETE | `/automatic-memories/{memory_id}` | 202，本地立即停止使用，外部后台清理 |
| DELETE | `/automatic-memories` | 202，切换代际、后台清空；保留用户当前开关状态 |

保持现有 JWT 鉴权与错误结构。请求体没有 user_id，读取响应为 `Cache-Control: no-store`。

## 验证与限制

单元测试覆盖默认关闭、无历史回填、团队/评测排除、异步发布、关闭与代际、墓碑、失效引用、服务故障、未知写入结果和等待远程任务清理。真实 PostgreSQL 测试覆盖事务回滚、同用户顺序、跨用户并行领取、租约 fencing 与旧代际禁止发布。

已先用虚构凭据验证建会话、消息、读取与删除协议，见 [HTTP 记录](validation/openviking-protocol.json)，随后经用户明确授权在独立测试环境执行真实提取、问答与检索实验。逐题记录、原始余额中断失败、续跑策略及实际门禁见 [真实评测报告](validation/memory-live-20261029/report.md)。生产配置与原项目数据库未在该实验中启用或迁移。

模型供应商返回 402 时，问答接口使用 `MODEL_BALANCE_EXHAUSTED` / HTTP 503，保留内部原因链并隐藏上游正文；不重试或自动切换模型掩盖余额不足。评测客户端识别该稳定错误码并停止继续提交，保留已经完成和失败的记录。后台提取失败仍通过记忆同步状态呈现，不能显示“已记住”。

可复现实验与验收阈值见 [memory evaluation README](../evals/memory/README.md)。外部论文或 OpenViking 官方跑分不能当成本项目实测。
