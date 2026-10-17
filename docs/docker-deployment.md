# Ubuntu Docker 部署

本配置部署 FastAPI 单 worker 和 Nginx + Vue 构建产物。PostgreSQL、Milvus 使用已有外部服务；不包含本地模型、数据库和向量库容器。2 GiB 主机只适合轻量试用，实际内存取决于并发与文档大小。默认使用独立持久入库 Worker，与 API 共享上传/解析卷。

启用问答持久执行时，使用 `compose.full.yaml` 的独立 Run Worker，并先完成当前迁移。[完整配置启动与预检](deployment-preflight.md#docker-完整配置)包含首次共享卷初始化顺序；Run Worker 的并行槽数由 `CHAT_RUN_WORKERS` 控制。

## 1. 检查服务器

SSH 登录 Ubuntu，执行：

```bash
docker version
docker compose version
free -h
df -h /
```

确认 Docker 服务和 Compose 可用。权限不足时为 docker 命令加 sudo。不要重装或清理服务器上已有的数据库容器。

## 2. 上传源码

在本地 Windows PowerShell 的项目根目录执行（将 YOUR_USER 换成实际 SSH 用户）：

```powershell
tar -czf "$env:TEMP/learning-qa-deploy.tar.gz" Dockerfile compose.yaml .dockerignore pyproject.toml uv.lock alembic.ini migrations app compose.full.yaml frontend/Dockerfile frontend/nginx.conf frontend/.dockerignore frontend/package.json frontend/package-lock.json frontend/index.html frontend/src frontend/tsconfig.json frontend/vite.config.ts .env.example
scp "$env:TEMP/learning-qa-deploy.tar.gz" YOUR_USER@47.106.170.205:~/
```

此归档不包含本地 .env、虚拟环境、node_modules 或已上传资料。如果前端后续增加 public 等构建资源，需一并打包。也可通过私有 Git 仓库上传完整源码。

在服务器执行：

```bash
mkdir -p ~/learning-qa
cd ~/learning-qa
tar -xzf ~/learning-qa-deploy.tar.gz
# 仅首次部署复制模板；已有 .env 时保留
test -f .env || cp .env.example .env
chmod 600 .env
nano .env
```

必须填写 DATABASE_URL（保留 sslmode=require 或更严格的 TLS）、MILVUS_URI、必要的 MILVUS_TOKEN、DASHSCOPE_API_KEY、DEEPSEEK_API_KEY、JWT_SECRET。新部署生成独立的 JWT_SECRET；迁移既有服务时更换密钥将使原 access token 失效。

关键配置示例（其他项目配置保留）：

```dotenv
FRONTEND_ORIGIN=http://localhost:8080
AUTH_COOKIE_SECURE=false
REGISTRATION_ENABLED=false
DEFAULT_MODEL_PROVIDER=deepseek
ALLOW_MODEL_FALLBACK=false
```

默认仅通过 SSH 隧道试用。若需要首次创建账号，可临时开启 REGISTRATION_ENABLED=true，创建完成后关闭并重建 backend。现有账号随 PostgreSQL 数据保留。

容器内 localhost 指向该容器自身，不能直接使用本地电脑的 localhost 地址。外部服务必须允许云服务器访问；同一 Docker 网络中的已有数据库应使用服务名，宿主机数据库需要专门配置可达地址与访问权限。不要把数据库管理端口直接开放给所有公网 IP。

密钥只在服务器 .env 中配置，不写入镜像。Compose env_file 会注入运行环境；镜像构建不会复制 .env。

当前 Compose 已提供独立入库 Worker，API/Worker 均为 durable。完整已有功能的开启、只读预检与迁移步骤见 [部署预检](deployment-preflight.md)；不要只改开关而跳过迁移。

## 3. 构建并启动

2 GiB 服务器按顺序构建，减少构建阶段的并发内存占用：

```bash
cd ~/learning-qa
docker compose build backend
docker compose build ingestion-worker
docker compose build frontend
```

首次使用全新数据库或集合时，显式初始化（已有存储先确认目标地址并备份；无需每次部署执行）：

```bash
docker compose run --rm backend python -m app.bootstrap --postgres
docker compose run --rm backend python -m app.bootstrap --milvus
```

如启用额外特性，请按 .env.example 说明使用对应初始化参数。不会自动搬迁本地文档、向量和数据库数据；需要迁移时应单独备份并恢复这些数据，保持其一致性。

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100 backend ingestion-worker frontend
curl -fsS http://127.0.0.1:8080/health/ready
```

ready 应返回 PostgreSQL、Milvus、模型配置和入库 Worker 均为 ok。API healthcheck 仅检查进程存活；独立 Worker healthcheck 检查该容器自身的持久心跳，均不能代替完整 readiness。模型配置检查不代表上游 API 调用已成功；最后还需登录、上传一份小文档并验证一次问答。

如果构建被 OOM 杀死，可在另一台 Linux/amd64 构建机或 CI 构建镜像后上传，不要依赖 swap 来运行本地大模型。拉取 Docker Hub、GHCR、PyPI、npm 均需要服务器网络可达；失败时查看具体构建日志。

## 4. 访问

默认服务只监听服务器回环地址，适合先验证。在本地 Windows 新开 PowerShell 并保持连接：

```powershell
ssh -N -L 8080:127.0.0.1:8080 YOUR_USER@47.106.170.205
```

浏览器打开 http://localhost:8080 。前端和 API 同源，流式回答在 Nginx 中已关闭代理缓冲。

公网短期测试时，在 .env 设置 WEB_BIND=0.0.0.0、WEB_PORT=8080、FRONTEND_ORIGIN=http://47.106.170.205:8080，然后执行 docker compose up -d，并在阿里云安全组仅向自己的公网 IP 放行 TCP 8080。此方式仍是 HTTP。

正式开放使用域名和 HTTPS：在宿主机现有反向代理上配置证书，转发至 127.0.0.1:8080，并关闭该代理的 SSE 缓冲、设置足够的读取超时；设置 FRONTEND_ORIGIN=https://你的域名 和 AUTH_COOKIE_SECURE=true，执行 docker compose up -d 更新环境。保持 WEB_BIND=127.0.0.1，只开放 HTTPS 入口。若还有上游代理，需保证其上传限制与应用一致。

## 5. 更新与维护

上传更新后的源码后：

```bash
docker compose build backend
docker compose build ingestion-worker
docker compose build frontend
docker compose run --rm backend python -m app.migrate
docker compose up -d
docker compose logs --tail=100 backend
docker stats --no-stream
docker system df
```

仅修改 .env 后执行 docker compose up -d；docker compose restart 不会加载修改后的容器环境。

上传和解析文件放在 app_data 命名卷中，普通重建容器会保留。不要执行 docker compose down -v，这会删除命名卷。数据库和 Milvus 不在本 Compose 中，备份策略需单独设置。若迁移旧上传目录，文件需写入该卷并归 UID 10001 所有；单纯上传源码不会迁移资料。

镜像不包含 LibreOffice，旧版 DOC 本地转换不可用；需要该格式时配置项目支持的 MinerU 云解析，或另行增加 LibreOffice。默认 Nginx 上传上限 21m，与项目默认 20 MiB 文件上限相配；提高 MAX_UPLOAD_MB 时同时调整 nginx.conf。
