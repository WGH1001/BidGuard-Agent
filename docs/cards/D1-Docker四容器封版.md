# D1 — Docker 四容器封版 + 模型测速延迟显示

> 执行环境：无工作区、无法运行命令。一切以附件为准，**不要访问任何仓库或联网拉取代码**。
> 验收由架构侧本地完成（构建镜像、起容器、跑测试），你只负责输出代码文件。

## 0. 背景（30 秒版）

项目 = 投标方标书防废标体检智能体。引擎层 F1~F7、backend 十组路由、前端 8 视图均已完成验收入库。
开发态：后端 uvicorn + SQLite（`data/bidguard.db`），前端已构建为 `frontend/dist` 由后端托管。
本次目标：**四容器生产化封版（nginx + app + postgres16 + redis7）**，附带一个模型测速的小改造。

三个已核实的技术事实（写代码前必读）：

1. `backend/database.py` 对 `DATABASE_URL` 的非 sqlite 值**直接放行**（只对 sqlite 做路径锚定）——
   所以接 Postgres **不需要改 database.py / models.py**，只需 requirements 加驱动 + compose 注入连接串；
2. **redis 当前代码零消费**（`REDIS_URL` 无任何代码读取）——容器按"预留"处理，compose 里保留 + 健康检查即可，
   **严禁为了"用上 redis"新增任何业务代码**；
3. models.py 的 JSON 列、种子数据（tenant/user/rule）与 85 项测试在 Postgres 下由架构侧验收，你保证
   驱动声明与 env 样例正确即可。

## 1. 交付物（10 个文件）

| # | 文件 | 性质 | 要求 |
|---|---|---|---|
| 1 | `requirements.txt` | 完整覆盖版 | 取消 `psycopg2-binary==2.9.9` 与 `redis==5.*` 两行注释（归入"生产部署依赖"段），其余一字不动 |
| 2 | `backend/schemas.py` | 完整覆盖版 | 仅一处改动：`ModelConfigTestOut` 增加 `latency_ms: int = 0` 字段，其余全部原样 |
| 3 | `backend/routers/model_config.py` | 完整覆盖版 | 仅改 `/test` 端点：入口记 `t0 = time.perf_counter()`，收尾算 `latency_ms = int(round((time.perf_counter()-t0)*1000))`；成功分支 message 按蓝本格式带延迟（如 `鉴权通过（823ms，模型：qwen3.8-flash）`）；返回值加 `latency_ms`。参照蓝本 552~631 行的计时与分支写法，适配本项目现有分支结构 |
| 4 | `frontend/src/api/types.ts` | 完整覆盖版 | 仅一处改动：`ModelConfigTestOut` 增加 `latency_ms?: number`，其余原样 |
| 5 | `frontend/src/views/ModelSettingsView.vue` | 完整覆盖版 | `handleTest`：`res.ok` 时若后端 message 为空则兜底显示 `` `配置校验通过（${res.latency_ms ?? 0}ms）` ``，message 非空直接展示（后端已含延迟）；warning 分支同理。**保留现有全部内容**（PRESETS 的 key/provider_name、selectedKey 高亮、currentProviderName、preset-note 等一个都不能丢） |
| 6 | `Dockerfile` | 新建 | 以蓝本为底改造：python:3.12-slim；装 requirements（含 psycopg2-binary/redis）；COPY `core/ backend/ config/ requirements.txt` 与**本地已构建的 `frontend/dist`**（前端在宿主机 npm run build 后随镜像 COPY，不在容器内装 node，除非蓝本本身就是多阶段 node 构建——照蓝本姿势）；默认 CMD uvicorn backend.main:app --host 0.0.0.0 --port 8000 |
| 7 | `docker-compose.yml` | 新建 | 四服务：①`app`（build .，注入 `DATABASE_URL=postgresql+psycopg2://bidguard:${POSTGRES_PASSWORD}@postgres:5432/bidguard`、`REDIS_URL=redis://redis:6379/0`，挂 `app-data:/app/data` 与 `app-output:/app/output`，depends_on postgres/redis healthy）②`postgres`（postgres:16-alpine，env 用 `${POSTGRES_USER:-bidguard}/${POSTGRES_PASSWORD}/${POSTGRES_DB:-bidguard}`，volume `pgdata`，healthcheck `pg_isready`）③`redis`（redis:7-alpine，appendonly 卷，healthcheck）④`nginx`（80 端口映射宿主 `8000:80`，挂 `deploy/nginx/nginx.conf` 与 `frontend/dist` 只读，depends_on app） |
| 8 | `deploy/nginx/nginx.conf` | 新建 | 以蓝本为底：前端静态托管（dist 根，SPA history 回退 try_files ... /index.html）；`/api/` 反代 `app:8000`；**SSE 必需** `proxy_buffering off` + `proxy_read_timeout 3600s`（/api/checks/*/stream 路径单独 location 也要覆盖到）；`client_max_body_size 100m`（上传上限对齐后端） |
| 9 | `deploy/.env.docker.example` | 新建 | compose 消费的变量样例：`POSTGRES_USER` / `POSTGRES_PASSWORD`（强密码占位）/ `POSTGRES_DB` / `TZ=Asia/Shanghai`，带注释说明 `cp deploy/.env.docker.example .env` 后使用 |
| 10 | `docs/DEPLOY.md` | 新建 | 部署手册：前置条件（Docker 安装检查、**构建前关闭系统代理**——镜像源 401 与代理强相关）、`npm run build` → `cp .env` → `docker compose up -d --build` 一键流程、健康检查（`curl http://127.0.0.1:8000/api/health` 与容器状态）、数据备份（pgdata 卷 + data/output 卷）、常见排查（端口占用 / 构建时代理 401 / SSE 卡住查 nginx buffering / 容器时区）、**双模式说明**（容器化 PG 模式 vs 本机 SQLite 直跑模式，DATABASE_URL 怎么切） |

## 2. 铁律

1. **不修改 core/ 下任何文件**；backend 只允许动 `schemas.py` 与 `routers/model_config.py` 两处、且只做上文指定的最小改动——`models.py / database.py / auth.py` 一字不动（发现疑似契约冲突停下来报错，不要自行改）；
2. 前端两个文件是**完整覆盖版**输出：以附件基线为基础只做指定追加，C1~C3 既有内容（含 ModelSettingsView 的模板高亮与 provider 标签）严禁丢失；
3. redis 容器是**预留位**：只出现在 compose 与 DEPLOY.md，不新增任何消费代码；
4. compose/env 里的密码占位必须用强密码示例（如 `ChangeMe_2026_Str0ng!`），严禁弱口令示例；敏感项一律走环境变量注入，不写死；
5. 中文注释、UTF-8；配置文件里路径用 POSIX 斜杠（容器内 Linux）；YAML 缩进两空格；
6. 你无法运行 docker/npm/pytest，验收在本地——**每个文件输出完整全文，严禁截断、严禁省略号占位**；若接近单次输出上限，在文件边界停下说"待续"，严禁把一个文件切成两半。

## 3. 分批输出

- **第一批**（约 1200 行）：交付物 1~5（requirements / schemas / model_config / types / ModelSettingsView 完整覆盖版）。输出完停，等"继续"。
- **第二批**（约 800 行）：交付物 6~10（Dockerfile / docker-compose / nginx.conf / .env.docker.example / DEPLOY.md）。

## 4. 验收口径（架构侧本地执行，写代码时对齐这些点）

- `docker compose up -d --build` 后 `docker ps` 四容器 healthy；
- `curl http://127.0.0.1:8000/api/health` 通、浏览器 80 端口出登录页（SPA 刷新 /checks 不 404）；
- 85 项 pytest 以 `DATABASE_URL` 指向 postgres 容器全绿，重启容器数据不丢（pgdata 卷生效）；
- 上传双书 → SSE 进度实时推进（nginx 不缓冲）→ 风险卡/偏离表全流程可用；
- `/test` 返回 `latency_ms` 且页面成功消息带"（xxxms）"。
