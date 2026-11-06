# BidGuard-Agent · Docker 四容器生产部署与运维手册

本文档提供 **BidGuard-Agent（投标方标书防废标体检与偏离表生成智能体）** 的四容器生产化部署流程、双运行模式切换、数据卷备份与常见故障排查指南。

---

## 1. 架构拓扑概览

生产环境采用 **Docker Compose 四容器编排**：

| 容器名称 | 镜像 / 构建源 | 职责说明 | 端口与持久化卷 |
|---|---|---|---|
| `bidguard-nginx` | `nginx:alpine` | 对外唯一 Web 入口：静态直发 `frontend/dist`（含 SPA 路由回退）+ `/api/` 反代至 `app:8000`（配置 SSE 零缓冲与 `100m` 上传上限） | 宿主机 `${APP_PORT:-8000}:80` |
| `bidguard-app` | 本地 `Dockerfile` (`python:3.12-slim`) | 核心引擎 F1~F7 + FastAPI 十组路由 + RapidOCR 离线识别 + 后台核验线程池 | 内部 `8000`（不直接暴露外网）；挂载 `app-data:/app/data`、`app-output:/app/output` |
| `bidguard-postgres` | `postgres:16-alpine` | 生产关系型主库（存储 9 张核心业务表：租户、用户、任务、风险项、规则、证照、会话、审计、模型配置） | 绑定本机回环 `127.0.0.1:5432:5432`；挂载 `pgdata:/var/lib/postgresql/data` |
| `bidguard-redis` | `redis:7-alpine` | 队列与缓存预留容器（开启 AOF 持久化 `--appendonly yes`） | 内部 `6379`；挂载 `redis-data:/data` |

---

## 2. 部署前置条件（必读）

1. **检查 Docker 与 Compose 环境**：
   ```powershell
   docker --version
   docker compose version
   ```
2. **⚠️ 构建前务必关闭本机系统代理 / VPN 代理软件（关键避坑）**：
   - 在执行 `docker compose build` 或拉取基础镜像前，**请务必关闭本机 Clash / v2rayN 等系统代理及 TUN 模式**，并确保终端未残留 `HTTP_PROXY` / `HTTPS_PROXY` 环境变量；
   - **原因**：开启系统代理时，Docker Desktop 守护进程向镜像源或阿里云源发起请求极易被代理劫持或改写凭证，导致报 `401 Unauthorized`、`TLS handshake timeout` 或 `apt-get` 连接失败。

---

## 3. 一键构建与启动流程

在项目根目录（`E:\agent\BidGuard-Agent`）按顺序逐行执行以下三步：

### 第一步：在宿主机编译前端生产包 (`frontend/dist`)
`Dockerfile` 与 `nginx` 容器均直接挂载/打包宿主机已构建好的 `frontend/dist` 静态产物：
```powershell
cd frontend
npm run build
cd ..
```

### 第二步：复制并配置 Docker 环境变量文件 (`.env`)
```powershell
Copy-Item deploy/.env.docker.example .env
```
*(可按需打开 `.env` 修改 `POSTGRES_PASSWORD`、`AUTH_SECRET` 或填入 `ZHIPU_API_KEY`；亦可在启动后登录前端「私有模型服务配置」页面在线绑定 API Key)*。

### 第三步：启动四容器编排
```powershell
docker compose up -d --build
```

---

## 4. 健康检查与验收核验

1. **查看四容器运行与 Healthy 状态**：
   ```powershell
   docker compose ps
   ```
   正常情况下，`bidguard-nginx`、`bidguard-app`、`bidguard-postgres`、`bidguard-redis` 四个容器的 `STATUS` 均应显示为 `Up ... (healthy)`。

2. **验证 Nginx 入口与后端健康探针**：
   ```powershell
   curl [http://127.0.0.1:8000/api/health](http://127.0.0.1:8000/api/health)
   ```
   预期返回：
   ```json
   {"status":"ok","service":"bidguard-agent","version":"1.0.0"}
   ```

3. **浏览器访问与 SPA 路由刷新验证**：
   - 打开浏览器访问 `http://127.0.0.1:8000`；
   - 使用预置演示账号登录（管理员：`admin / BidGuard2026!`，投标商务经理：`liming / BidGuard2026!`）；
   - 进入 `/checks` 或 `/deviation` 页面后按 `F5` 刷新浏览器，验证 Nginx `try_files` History 路由回退正常（不出现 404）。

---

## 5. 双模式切换说明（容器化 PostgreSQL 模式 vs 本机 SQLite 直跑模式）

`backend/database.py` 内置了双驱动无缝识别机制：当 `DATABASE_URL` 为 `sqlite:///...` 时自动锚定项目根并开启 WAL 与外键级联；当 `DATABASE_URL` 为 `postgresql+psycopg2://...` 时自动启用 PostgreSQL 连接池（`pool_pre_ping=True`）。

### 模式 A：Docker 四容器生产模式（PostgreSQL 16）
- 由 `docker-compose.yml` 自动向 `app` 容器注入：
  ```ini
  DATABASE_URL=postgresql+psycopg2://bidguard:ChangeMe_2026_Str0ng!@postgres:5432/bidguard
  ```
- 若需在**宿主机本地**运行全量 `pytest` 直接验证容器内的 PostgreSQL 16 数据库（通过 `127.0.0.1:5432` 回环映射）：
  ```powershell
  $env:DATABASE_URL="postgresql+psycopg2://bidguard:ChangeMe_2026_Str0ng!@127.0.0.1:5432/bidguard"
  .venv\Scripts\python -m pytest tests/ -q
  ```

### 模式 B：宿主机本地开发直跑模式（SQLite 零依赖回退）
- 在不启动 Docker 容器的开发场景下，直接清除或将环境变量设回 SQLite：
  ```powershell
  Remove-Item Env:\DATABASE_URL -ErrorAction SilentlyContinue
  .venv\Scripts\python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --env-file .env
  ```
  此时后端自动使用本地 `data/bidguard.db` 文件库运行。
- **`--env-file .env` 说明**：本机直跑模式默认**不会**自动读取项目根的 `.env`（代码无 dotenv 逻辑），
  必须显式加 `--env-file .env`（uvicorn 内置参数）才会加载——推荐始终带上，让 `.env` 成为唯一配置源。
  注意优先级：**系统/用户级环境变量 > .env 文件**（uvicorn 加载 dotenv 默认不覆盖已存在的系统变量），
  若某变量改了 `.env` 却不生效，检查 Windows「环境变量」里是否有同名系统级残留（如历史设置的
  `ZHIPU_API_KEY`），删掉系统级那条即可让 `.env` 接管。`启动BidGuard.bat` 已内置该参数（.env 存在时自动启用）。

---

## 6. 数据持久化与备份恢复

系统共配置 4 个命名数据卷（容器重启或重建镜像均不会丢失数据）：

1. **备份 PostgreSQL 数据库 (`pgdata`)**：
   ```powershell
   docker exec bidguard-postgres pg_dump -U bidguard -d bidguard > backup_bidguard_$(Get-Date -Format 'yyyyMMdd').sql
   ```
2. **恢复 PostgreSQL 数据库**：
   ```powershell
   Get-Content backup_bidguard_20261106.sql | docker exec -i bidguard-postgres psql -U bidguard -d bidguard
   ```
3. **备份上传标书原件 (`app-data`) 与导出偏离表/交付包 (`app-output`)**：
   ```powershell
   docker cp bidguard-app:/app/data ./backup_app_data
   docker cp bidguard-app:/app/output ./backup_app_output
   ```

---

## 7. 常见故障排查清单（Troubleshooting）

1. **`docker compose build` 拉取镜像或安装包时报 `401 Unauthorized` / `EOF` / 网络超时**：
   - **原因**：本机开启了系统代理软件（或配置了失效的 Docker Registry Mirror）。
   - **解决**：关闭系统代理软件与 TUN 网卡，重启 Docker Desktop 后重新执行 `docker compose up -d --build`。

2. **启动时报端口 `8000` 或 `5432` 已被占用（`bind: address already in use`）**：
   - **原因**：宿主机本地仍在运行 `uvicorn` 进程（占用了 8000）或本地安装了 Windows 版 PostgreSQL 服务（占用了 5432）。
   - **解决**：停止宿主机上的 `uvicorn` 进程；或在 `.env` 中修改 `APP_PORT=8080` 改道对外端口。

3. **上传双书启动核验后，前端 SSE 进度条卡在初始状态不实时推进，直到最后才一次性跳到 100%**：
   - **原因**：反向代理层开启了响应缓冲（Buffering）。
   - **解决**：确认 `deploy/nginx/nginx.conf` 中 `/api/checks/[0-9]+/stream` 与 `/api/` 块均已包含 `proxy_buffering off;`、`proxy_cache off;` 与 `proxy_read_timeout 3600s;`，修改后执行 `docker compose restart nginx`。

4. **修改了 `.env` 中的 `POSTGRES_PASSWORD`，但 `app` 容器报数据库连接 `password authentication failed`**：
   - **原因**：PostgreSQL 官方镜像仅在 `pgdata` 数据卷首次为空时读取 `POSTGRES_PASSWORD` 初始化账密；已有数据卷时仅改 `.env` 不会改变库内已有密码。
   - **解决**：保持 `.env` 密码与首次建卷时一致；或在确认无需保留旧测试数据时执行 `docker compose down -v` 清理旧卷后重新 `docker compose up -d`。

5. **审计日志或导出偏离表上的时间戳与北京时间相差 8 小时**：
   - **解决**：确认 `.env` 中已设置 `TZ=Asia/Shanghai`，且 `docker-compose.yml` 已向容器注入 `TZ` 环境变量。