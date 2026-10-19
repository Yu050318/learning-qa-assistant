# 模型端点兼容维护

2026-10-18 隔离评测预检发现，官方模型目录已不包含 `deepseek-chat`。DeepSeek [更新记录](https://api-docs.deepseek.com/updates/)说明旧名称于 2026-07-24 停用，当前 Flash 使用 `deepseek-flash`。新环境默认值和 `.env.example` 已更新。

原有安装不会在启动时改写数据库模型配置。升级前先备份数据库和 `.env`，执行项目迁移，再由运维明确执行：

```powershell
python -m app.migrate
python -m app.bootstrap --migrate-retired-deepseek-chat
```

该维护操作只接受 `api.deepseek.com` 端点，只迁移 DeepSeek Profile 中的 `deepseek-chat`；自定义代理请在模型中心探测、确认名称后手动修改。旧配置历史与已创建 Run 的冻结快照保留，其他模型、角色分配和用户权限保持原值。修改产生新配置修订；目录格式的 Profile 版本递增；重复执行没有待迁移项时不新增修订。迁移会清空旧健康探测结果，完成后重新探测可用性。

同时检查服务端 `.env` 的 `DEEPSEEK_MODEL`；若仍为旧名称，显式改为 `deepseek-flash`，然后重启 API 与各 Worker。数据库已启用模型中心时，环境变量不会覆盖数据库中的目录，因此两处都需要核对。旧模型冻结的未完成任务不会被静默换成其他模型，应先处理在途任务。

适配器对 `deepseek-flash` 显式发送 `thinking.type=disabled`，保持原 chat 的非思考式调用约定，避免新模型默认思考模式改变工具交互和输出预算。此参数遵循[官方思考模式文档](https://api-docs.deepseek.com/guides/thinking_mode/)，不会发送给其他模型名称。
