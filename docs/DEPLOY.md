# BidGuard-Agent · 本机与公司 Linux 服务器部署运维手册

本文档提供 **BidGuard-Agent（投标方标书防废标体检与偏离表生成智能体）** 的全场景部署指南，涵盖：
- **场景一**：本机开发与演示直跑（Windows / Linux + SQLite 零依赖）
- **场景二**：Docker 四容器标准部署（Windows Desktop / 公司 Linux 服务器）
- **场景三**：公司内网隔离/无外网 Linux 机房离线迁移（`docker save` / `docker load`）
- **场景四**：公司 Linux 服务器裸机无 Docker 部署（Python venv + Systemd + Nginx）
- **滚动升级、PostgreSQL 数据备份与常见故障排查**

---

## 1. 架构拓扑概览（四容器编排）

生产环境默认采用 **Docker Compose 四容器编排**（见根目录 `docker-compose.yml`）：

| 容器名称 | 镜像 / 构建源 | 职责说明 | 端口与持久化卷 |
|---|---|---|---|
| `bidguard-nginx` | `nginx:alpine` | 对外唯一 Web 入口：静态直发 `frontend/dist`（含 SPA 路由回退）+ `/api/` 反代至 `app:8000`（配置 SSE 零缓冲与 `100m` 上传上限） | 宿主机 `${APP_PORT:-8000}:80` |
| `bidguard-app` | 本地 `Dockerfile` (`python:3.12-slim`) | 核心引擎 F1~F7 + FastAPI **11 组路由** + RapidOCR 离线识别 + 后台核验线程池 | 内部 `8000`；挂载 `app-data:/app/data`、`app-output:/app/output`、`./config:/app/config:ro`、`./data/corpus/regulations:/app/data/corpus/regulations:ro` |
| `bidguard-postgres` | `postgres:16-alpine` | 生产关系型主库（存储 **11 张核心业务表**：租户、用户、任务、风险项、规则、证照、会话、审计、模型配置、法规主表、法规条文表） | 绑定回环 `127.0.0.1:5432:5432`；挂载 `pgdata:/var/lib/postgresql/data` |
| `bidguard-redis` | `redis:7-alpine` | 预留扩展容器（开启 AOF 持久化 `--appendonly yes`） | 内部 `6379`；挂载 `redis-data:/data` |

---

## 2. 场景一：本机开发与演示直跑（SQLite 零依赖）

适合研发调试或单机临时演示，无需启动 PostgreSQL/Redis，后端自动在项目根目录下生成并使用 `data/bidguard.db`（WAL 模式 + 外键级联）。

### 2.1 一键静态单口直跑（推荐演示）

```powershell
# Windows（PowerShell）
# 1. 若尚未编译前端或拉取了前端新代码，先构建静态产物
npm --prefix frontend run build

# 2. 首次准备 .env（复制模板并按需填写 ZHIPU_API_KEY / PRIVATE_API_KEY）
Copy-Item .env.example .env

# 3. 方式 A：双击或运行项目根目录启动脚本（自动检测并加载 .env）
.\启动BidGuard.bat

# 3. 方式 B：手动启动
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --env-file .env
```

Linux 本机等价启动（确保 `frontend/dist` 已编译且已创建 `.venv`）：
```bash
.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --env-file .env
```

### 2.2 前后端分离热更新开发（开发调试）

- **终端 1（后端 FastAPI 热重载）**：
  ```powershell
  .venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload --env-file .env
  ```
- **终端 2（前端 Vite 热更新，代理 `/api` 至 8000）**：
  ```powershell
  npm --prefix frontend run dev
  ```
- 浏览器访问 `http://127.0.0.1:5173`。

> **重要环境变量优先级提示**：
> 1. 后端代码本身不自动加载 `.env`，必须带 `--env-file .env`；
> 2. 宿主机**系统/用户环境变量优先级高于 `.env` 文件**。如果修改了 `.env` 却发现没生效，检查系统环境变量里是否残留了同名旧值（如全局 `ZHIPU_API_KEY`）；
> 3. `config/models.yaml` 中仅定义 `demo`（智谱外网）与 `prod`（公司内网网关）两档，`MODEL_ENV` 应填 `demo` 或 `prod`（不要填 `private`）。

---

## 3. 场景二：Docker 四容器标准部署（Windows / 公司 Linux 服务器）

### 3.1 前置检查（必读两大避坑）

1. **检查 Docker 与 Node.js**：
   ```bash
   docker --version
   docker compose version
   node -v && npm -v
   ```
2. **编译 `frontend/dist`（必须在 `docker compose build` 之前）**：
   `Dockerfile` 第 37 行会执行 `COPY frontend/dist/ /app/frontend/dist/`，`nginx` 容器也会挂载 `./frontend/dist`。若直接克隆仓库未构建就执行 `docker compose up --build`，会直接报 `COPY failed: file not found`！
3. **⚠️ 本机/服务器构建前关闭代理软件（关键避坑）**：
   执行 `docker compose build` 拉取基础镜像或安装依赖前，务必关闭 Clash / v2rayN 系统代理及 TUN 模式，并清理终端 `HTTP_PROXY` / `HTTPS_PROXY` 变量；否则容器守护进程极易报 `401 Unauthorized`、`TLS handshake timeout` 或 `apt-get` 失败。

### 3.2 Linux 服务器（及 Windows Docker）上线操作流

以公司 Linux 服务器（Ubuntu / Debian / CentOS / Rocky）为例：

```bash
# 1. 进入项目根目录
cd /opt/BidGuard-Agent

# 2. 安装前端依赖并编译生产包（约 8~15 秒）
npm --prefix frontend ci
npm --prefix frontend run build

# 3. 复制 Docker 环境变量模板并修改生产安全参数
cp deploy/.env.docker.example .env
vim .env
```

### 3.3 公司生产环境 `.env` 必改安全清单

打开 `.env`，上线到公司内网或公网服务器时**至少确认以下 9 项**：

| 变量名 | 生产推荐值 | 说明与避坑警告 |
|---|---|---|
| `POSTGRES_PASSWORD` | 自定义强密码 | 仅 `pgdata` 卷**首次初始化**时生效，后续修改需进库 `ALTER USER` |
| `AUTH_SECRET` | 32 位以上随机字符串 | 可在 Linux 运行 `openssl rand -hex 32` 生成；配合 `DEBUG=0` 使用 |
| `DEBUG` | `0` | 设为 `0` 时 `backend/auth.py` 会强校验 `AUTH_SECRET`，未改默认值会直接拒绝启动 |
| `ADMIN_INITIAL_PASSWORD` | 自定义强密码 | 默认是 `BidGuard2026!`，首次建库前务必改成内部管理员初始密码 |
| `SEED_DEMO_DATA` | **首次 `1`，建库后改 `0`** | **⚠️ 首次部署必保持 `1`**：`SEED_DEMO_DATA=0` 会跳过默认租户与 `admin` 账号初始化，导致空库无账号登录；首次启动登录确认后再改为 `0` 并重建 `app` |
| `ENABLE_API_DOCS` | `0` | 关闭 `/docs`、`/redoc` 与 `/openapi.json`，防止接口信息枚举 |
| `MODEL_ENV` | `prod` | 对应 `config/models.yaml` 内网网关地址（按公司实际地址调整 `config/models.yaml`）；如用各人前端自填 Key，可留默认并在网页端配 |
| `PRIVATE_API_KEY` | 内网模型密钥 | 与 `MODEL_ENV=prod` 配套使用（也可在网页「私有模型服务配置」按用户单独加密绑定） |
| `CORS_ORIGINS` | `http://服务器IP:8000` | 若绑定域名则改为 `https://bidguard.company.com`，多个逗号分隔 |
| `BIDGUARD_F4_CONCURRENCY` | `4`（内网限流可调 `2`） | F4 阶段 B 线程池并发度（默认 `4`；置 `1` 退回纯串行） |

保存 `.env` 后启动四容器：
```bash
# 首次构建并后台拉起四容器
docker compose up -d --build

# 查看容器状态（四容器全部显示 healthy 为正常）
docker compose ps
```

*(Windows PowerShell 等价命令：用 `Copy-Item deploy/.env.docker.example .env` 替代 `cp`，其余 `npm` 与 `docker compose` 命令一致。)*

---

## 4. 场景三：公司内网隔离/无外网 Linux 服务器离线迁移

如果公司内网服务器**完全断外网**（无法执行 `npm install`、`pip install` 或拉取 `python:3.12-slim` / `postgres` 镜像），采用**“联网打包机预先 build + `docker save/load` 迁移”**方案：

### 第一步：在有网 Linux 构建机（同 CPU 架构，通常 x86_64/amd64）打包

```bash
cd /path/to/BidGuard-Agent

# 1. 在构建机完成前端构建
npm --prefix frontend ci && npm --prefix frontend run build

# 2. 构建镜像并确保基础镜像已拉齐
docker compose build app
docker compose pull postgres redis nginx

# 3. 导出全套 4 个镜像为单个 tar 包（检查实际镜像名以 docker compose images 为准）
docker save -o bidguard-images-v1.tar \
  bidguard-agent-app:latest \
  nginx:alpine \
  postgres:16-alpine \
  redis:7-alpine
```
*(提示：若构建机上仓库目录名不是 `BidGuard-Agent`，`app` 镜像名会是 `<目录名小写>-app:latest`，可用 `docker compose images` 查看精确 `Repository:Tag`。)*

### 第二步：将代码目录与镜像包传输至内网 Linux 服务器

拷贝 `bidguard-images-v1.tar` 与整个项目目录（务必包含刚编出的 **`frontend/dist/`**、**`config/`**、**`deploy/`**、**`data/corpus/regulations/`**、**`docker-compose.yml`**）到内网服务器 `/opt/BidGuard-Agent`。

### 第三步：内网机房加载镜像并直接启动（零联网依赖）

```bash
cd /opt/BidGuard-Agent

# 1. 导入镜像
docker load -i bidguard-images-v1.tar

# 2. 按 §3.3 配置好 .env（内网 MODEL_ENV=prod + PRIVATE_API_KEY + 强密钥）
cp deploy/.env.docker.example .env
vim .env

# 3. 不触发构建直接拉起四容器
docker compose up -d --no-build
docker compose ps
```

---

## 5. 场景四：公司 Linux 服务器裸机无 Docker 部署（兜底参考）

若公司目标 Linux 服务器不允许安装 Docker，可采用 **Python 3.12 + Systemd + 宿主机 Nginx** 方案：

### 5.1 安装系统底层库（RapidOCR 依赖，缺一不可）

- **Debian / Ubuntu**：
  ```bash
  sudo apt-get update && sudo apt-get install -y python3.12 python3.12-venv libgl1 libglib2.0-0 libxcb1 nginx
  ```
- **CentOS / Rocky / RHEL**：
  ```bash
  sudo dnf install -y python3.12 mesa-libGL glib2 libxcb nginx
  ```

### 5.2 初始化运行环境与前端构建

```bash
cd /opt/BidGuard-Agent
python3.12 -m venv .venv
.venv/bin/pip install --no-cache-dir -r requirements.txt
npm --prefix frontend ci && npm --prefix frontend run build

cp .env.example .env
vim .env
# 生产裸机同样可配 PostgreSQL（DATABASE_URL=postgresql+psycopg2://...），不配则默认使用 SQLite ./data/bidguard.db
```

### 5.3 配置 Systemd 守护进程（`/etc/systemd/system/bidguard.service`）

```ini
[Unit]
Description=BidGuard-Agent FastAPI Service
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/BidGuard-Agent
ExecStart=/opt/BidGuard-Agent/.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --env-file /opt/BidGuard-Agent/.env
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用并启动服务：
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bidguard
sudo systemctl status bidguard
```

### 5.4 配置宿主机 Nginx（重点：必须关闭 SSE 缓冲）

直接参考 `deploy/nginx/nginx.conf`：将 `proxy_pass http://app:8000;` 改为 `proxy_pass http://127.0.0.1:8000;`，静态目录 `root` 改为 `/opt/BidGuard-Agent/frontend/dist;`，并**原样保留以下关键参数**：
- `client_max_body_size 100m;`（允许大招标文件上传）
- `proxy_buffering off;`、`proxy_cache off;`、`add_header X-Accel-Buffering no;`（防 SSE 进度流卡死）
- `proxy_read_timeout 3600s;`（防止大模型或长体检断开连接）
- `try_files $uri $uri/ /index.html;`（Vue History 路由刷新不报 404）

---

## 6. 日常运维：健康验证、滚动升级与数据备份

### 6.1 健康检查与验收

```bash
# 验证 Nginx 入口与后端服务探针
curl http://127.0.0.1:8000/api/health
# 预期返回：{"status":"ok","service":"bidguard-agent","version":"1.0.0"}
```

打开浏览器访问 `http://服务器IP:8000`（首次用 `admin` / 设定的初始密码登录），进入 `/checks` 后按 `F5` 刷新页面验证 SPA 路由不出现 404。

### 6.2 代码升级不丢数据流程（以拉取 E19 等新版本为例）

系统自 E11 起内置 `ensure_bid_check_tasks_columns()` 方言感知老库平滑补列机制，常规版本升级**无需执行 `down -v`，不会丢失数据库与标书原件**：

```bash
cd /opt/BidGuard-Agent
git pull origin main
npm --prefix frontend run build
docker compose up -d --build app nginx
docker compose ps
```

> **提示**：如想在宿主机用 `.venv` 直接对容器内的 PostgreSQL 跑全量回归测试（`postgres` 容器映射了 `127.0.0.1:5432`）：
> ```powershell
> $env:DATABASE_URL="postgresql+psycopg2://bidguard:ChangeMe_2026_Str0ng!@127.0.0.1:5432/bidguard"
> .venv\Scripts\python.exe -m pytest tests/ -q
> ```

### 6.3 数据备份与恢复（四个持久化命名卷）

1. **备份 PostgreSQL 数据库 (`pgdata`)**：
   ```bash
   # Linux
   docker exec bidguard-postgres pg_dump -U bidguard -d bidguard > backup_bidguard_$(date +%Y%m%d).sql
   ```
   ```powershell
   # Windows PowerShell
   docker exec bidguard-postgres pg_dump -U bidguard -d bidguard > backup_bidguard_$(Get-Date -Format 'yyyyMMdd').sql
   ```
2. **恢复 PostgreSQL 数据库**：
   ```bash
   cat backup_bidguard_20261110.sql | docker exec -i bidguard-postgres psql -U bidguard -d bidguard
   ```
3. **备份上传标书原件 (`app-data`) 与导出偏离表/交付包 (`app-output`)**：
   ```bash
   docker cp bidguard-app:/app/data ./backup_app_data
   docker cp bidguard-app:/app/output ./backup_app_output
   ```

---

## 7. 常见故障排查清单（Troubleshooting）

1. **`docker compose build` 报 `COPY failed: frontend/dist` 或拉包报 `401 / EOF`**：
   - 确认构建前已在宿主机运行 `npm --prefix frontend run build`；
   - 关闭宿主机 Clash/v2rayN 系统代理及 TUN 模式后重试。
2. **首次启动进入系统登录失败（或提示用户不存在）**：
   - 检查首次建库时 `.env` 是否误设了 `SEED_DEMO_DATA=0`。空库初始化必须保持 `SEED_DEMO_DATA=1` 才会写入默认租户和 `admin` 账号（建库后可改回 `0`）。
3. **容器一直重启，`app` 日志报 `RuntimeError` 关于 `AUTH_SECRET`**：
   - 当 `.env` 配置了 `DEBUG=0` 时，必须把 `AUTH_SECRET` 修改为非开发默认值的强随机串。
4. **启动报 `环境中未找到配置: private`**：
   - `config/models.yaml` 内网网关档位名称为 `prod`，请将 `.env` 中的 `MODEL_ENV` 改为 `prod`。
5. **端口 `8000` 或 `5432` 被占用（`bind: address already in use`）**：
   - 停止本机残留 `uvicorn` 或本地 PostgreSQL 服务；或在 `.env` 中将 `APP_PORT` 改为如 `8080`。
6. **体检进度条长期不动，直到最后突然跳 100%**：
   - 反向代理开启了缓冲。核查 Nginx `/api/` 与 `/api/checks/[0-9]+/stream` 均已配 `proxy_buffering off;`、`proxy_cache off;`、`proxy_read_timeout 3600s;`，并重启 nginx。
7. **修改 `.env` 中的 `POSTGRES_PASSWORD` 后 `app` 报 `password authentication failed`**：
   - PostgreSQL 官方镜像仅在 `pgdata` 数据卷首次为空时消费密码；若为旧卷，保持原密码或在无保留需求时 `docker compose down -v` 清卷重建。
8. **导出文档或日志时间戳慢 8 小时**：
   - 确认 `.env` 中已设置 `TZ=Asia/Shanghai`，且容器已接收 `TZ` 变量。
