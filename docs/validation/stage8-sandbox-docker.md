# 阶段八 Sandbox 驱动中间验收

2026-10-21，在 Docker Desktop Linux 引擎上完成 **20 项真实容器检查**。本记录仅证明固定容器驱动和隔离协议，不代表第八阶段或生产 rootless 部署完成。逐项结果、镜像身份和源码摘要见 [结构化证据](stage8-sandbox-docker.json)。

## 已验证

- Python、sh 成功执行，文件经 stdin/stdout 协议返回；没有宿主目录挂载或宿主解包。
- 每次执行检查 Docker 实际 HostConfig：无网络、只读根、私有 PID/IPC、非 root、去能力、no-new-privileges、受限 tmpfs，以及 CPU/内存/进程限额。
- 负载内验证 uid 65532、父进程 PID 1、有效能力为零、seccomp 与 no-new-privileges 生效；根目录不可写、外网不可达、Docker socket 和应用目录不可见，固定子进程环境没有应用/数据库/模型凭据。
- JSON 形状的子进程输出不能把失败变为成功；输出洪泛、超时、取消和租约丢失均受控结束并清理容器。
- 脱离会话的后代进程被清理；符号链接、硬链接、FIFO、危险文件名、过深目录、文件数量及总量超限被拒绝，失败不导出文件。
- 单文件内核限制、内存限额和进程限额生效。每项检查结束都确认该次容器不存在；整组结束再次核实本应用 Sandbox 容器为零。
- 默认 `require_rootless=True` 明确拒绝当前非 rootless daemon；真实执行验收显式使用开发例外。没有宣称在生产 rootless daemon 上执行通过。

## 可重复执行

独立入口为 `tests/sandbox_docker_acceptance.py`，使用已有、按不可变身份固定的 Sandbox 镜像。将该文件传给可信 `run-worker` 镜像内的 `python -`，设置 `SANDBOX_TEST_IMAGE`。只有这个可信验收 Worker 获得本地 Docker socket；它不携带业务配置或凭据。受测负载容器由驱动创建，从不获得 socket。另以无网络临时容器核对可信 Worker 内 Sandbox 域、驱动和 runner 文件，摘要与当前工作树一致。

Windows 下直接从 Python 子进程访问 Docker 命名管道出现权限错误，因此采用 Linux 可信 Worker 验证，没有修改宿主管道 ACL。适配器单元及契约组合 **34 passed**，报告位于忽略目录 `data/validation/stage8-sandbox-driver.xml`。

随后后端完整回归 **717 passed、48 skipped、38 subtests passed**，报告 `data/validation/stage8-backend-sandbox-driver.xml`。跳过项仍需要各自的外部服务专项；本轮完整回归不代替后续数据库迁移、Run 集成或整阶段部署验收。

最初的单文件测试使用未显式关闭的 Python 缓冲文件对象，解释器销毁对象时的写入异常不影响退出码，不能作为预期失败的证据。测试已改为 `with` 明确关闭并传播写入错误，整组重新执行通过。没有以首次失败记录代替当前结果。

## 尚未完成

Sandbox 与能力目录、同一次 Run 的工具检查点/预算/授权复核尚未接入；异常退出后的遗留容器核查、数据库产物记录/鉴权下载、前端展示、完整时间线与 RAG 上下文、独立服务部署验收仍在后续范围。当前没有启用业务 Sandbox，没有业务迁移，也没有推送本阶段中间实现。任务完成后才按约定清理闲置任务镜像，保留最终部署镜像和现有数据库/数据卷。
