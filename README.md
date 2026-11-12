# BidGuard-Agent · 投标方标书防废标体检与偏离表生成智能体

<p align="center">
  <img src="https://img.shields.io/badge/Version-v1.0.0-blue?style=flat-square" alt="Version" />
  <img src="https://img.shields.io/badge/Tests-227%20Passed-success?style=flat-square&logo=pytest" alt="Tests" />
  <img src="https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Vue-3.5-4FC08D?style=flat-square&logo=vuedotjs&logoColor=white" alt="Vue3" />
  <img src="https://img.shields.io/badge/Docker-4%20Containers-2496ED?style=flat-square&logo=docker&logoColor=white" alt="Docker" />
  <img src="https://img.shields.io/badge/Engine-0--Token%20%2B%20LLM%20Hybrid-orange?style=flat-square" alt="Engine" />
</p>

> **卖方投标团队的「封标前最后一道安全锁 + 投前控标雷达」**
> 上传甲方招标文件与我方投标书正本，几分钟内完成全量废标项体检（资格/符合性/`★▲` 强制条款/报价算术/证照效期/串名穿帮/前后承诺矛盾），在投标书原位标红批注（附 PDF 总览跳转页），并一键生成带真实页码的《技术与商务偏离表.docx》；同时支持立项前扫描排他性控标参数、权威法规库法条三态真伪核验、标书专家多轮追问及招标文件所需证照清单自动提取与企业台账对账。

**V1.0.0 · 227 项全链路自动化测试全绿 · Docker 四容器生产部署（Nginx 入口 80 + app + PostgreSQL 16 + Redis 7）· 0-Token 确定性沙盒双轨引擎**

---

## 🎯 业务定位与实战痛点

在政企招投标实战中，卖方团队封标前常面临三大痛点：
1. **一票否决隐蔽雷区多**：数万字标书中漏答一条 `★` 实质性要求、分项报价乘加差 1 分钱、大小写金额不符、残留其他项目采购人名称（串名穿帮）或某本 ISO/等保证书在开标日恰好过期，都会直接导致**废标（一票否决）**。
2. **人工制作偏离表耗时易错**：面对数十乃至上百条 `★/▲` 技术与商务要求，人工逐条比对投标书响应位置、核填页码并排版《技术与商务偏离表.docx》往往耗时 1~2 天。
3. **投前立项难辨“萝卜坑”**：刚拿到招标书时，难以及时筛查出隐藏在技术参数中的指定品牌型号、非标物理尺寸或特定软著名称等排他性控标条款。

**BidGuard-Agent** 严守**卖方（投标方）防废标视角**，采用 **「0-Token 确定性规则/算术沙盒 + 受控大模型局部语义复核 + 原文逐字溯源锁账」** 双轨架构，在不编造、不漏检的前提下把 2 天的人工审标与制表工作压缩至几分钟内完成。

---

## ✨ 核心特性与功能矩阵（F1 ~ F7 全链路 + 法规库）

| 能力模块 | 名称 | 核心能力与技术实现 |
| :--- | :--- | :--- |
| 📊 **大盘态势** | **防废标体检总览** | 实时聚合部门在途项目、拦截一票否决硬伤数、已生成偏离表数、`★/▲` 召回率、企业证照临期预警与截标倒计时看板（ECharts 动态可视化）。 |
| 🚩 **F1** | **招标红线与 `★/▲` 提取器** | 100% 确定性抽取 `★/▲` 强制条款（自动区分 `requirement` 实质要求 / `usage_note` 说明语 / `table_marker` 表格碎片）、无标记废标措辞（模式 A/B）、资格与符合性审查项、表格型与段落型评分细则（含 100 分守恒校验）；**E23/E26 升级**：集成标书开标日期通用精准提取 + **招标所需证照清单 0-Token 确定性提取**（四大类分类、门槛/加分分层、逐条 `verify_quote` 溯源与免交护栏）。 |
| 💰 **F2** | **报价算术与大小写沙盒** | **100% 确定性 Python（0 Token）**：全程 `Decimal`（`ROUND_HALF_UP` 量化 `0.01` 元，严禁 `float`），自动识别开标一览表、多明细表小计聚合与分项汇总表，校验 `单价 × 数量 = 合价`、大小写金额一致性及是否突破最高投标限价（差 1 分钱必报警）；复杂版式自动触发 `NEED_MANUAL` 人工回填弹窗。 |
| 📜 **F3** | **证照效期与串名穿帮扫描** | **100% 确定性 Python（0 Token）**：对比项目开标日核查标书内证照有效期、社保缴纳月份连续区间、跨项目采购人串名自伤、`XXX/待填` 占位符残留及部门自定义禁用红线词；联动 Word 内嵌图片离线 RapidOCR 识别；**E26 升级**：与租户企业证照台账做四态保守对账（有效/临期/过期/未收录）。 |
| 🔍 **F4** | **`★/▲` 双书勾稽与自洽核验** | 四阶段条款对齐底座（`bid_aligner`）+ **三级自适应混合检索门控**（条款号/标题直通 → 45 组招投标同义词扩展 + BM25 → 向量语义精排）+ `MISSING_EVIDENCE` 截图/证明材料探针 + 标书内部质保期/工期前后承诺矛盾扫描；两阶段高并发提速（默认 4 路线程并发）。 |
| 📑 **F5** | **标准 Word 偏离表一键生成** | 严格消费 F4 逐条勾稽结果（同序对位、零二次篡改），一键生成符合政采规范的六列《技术与商务偏离表.docx》，支持 PDF 真实页码与 Word 双轨页码（印刷页码 / 物理页估算 / `@字符偏移` 诚实回退），负偏离与漏应答行自动红色加粗高亮；支持五维分类联动筛选与在途并发防锁死。 |
| 📡 **F6** | **投前控标雷达（立项决策流）** | 30 秒扫描招标书中的疑似品牌型号绑定、原厂唯一授权函、特定软著全称、毫米级非标尺寸等排他性参数（合法国产化/信创要求自动免检），输出带置信度与原文摘录的立项参考意见（强制标注“疑似”，辅助发起标前澄清提疑）；支持单条记录重新扫描与全站开标日期透出。 |
| 💬 **F7** | **标书专家多轮追问（ReAct）** | 绑定双书上下文与 **7 大受控工具**（含 `search_attachment` 附件检索与 `cite_regulation` 权威法条），支持**多文件与图片附件问答**（VLM 真实像素缩图与单实例拒图熔断）、SSE 思考轨迹流式直播与思考深度调节、0-Token 上下文动态快捷追问。 |
| ⚖️ **E4** | **招投标法规库与真伪核验** | 内置《招标投标法》《政府采购法》《87号令》等 8 部权威法规底表，支持法条三态核验（`exact_match` 逐字一致 / `fuzzy_hint` 近似或条号错位 / `unverifiable` 未收录，**大模型对法条真伪零裁决权**），支持管理员单条删除重算哈希、导入新规及官网更新检查。 |
| 🗂️ **规则与台账** | **企业投标证照与对账底表** | 支持企业证照效期台账管理，支持上传证照扫描件（PDF/图片）通过离线 OCR 自动预填证书名称、编号与到期日，原件安全留档与解绑；F1 提取结果一键自动四态对账并引导补录。 |
| 📦 **交付与审计** | **原位标红批注 + 三件套打包 + 审计 CSV** | 支持一键导出：① 投标书原件、② Word OOXML 批注气泡件 / PDF 原位高亮批注件（首部自动插入 A4 风险总览页并带 `LINK_GOTO` 点击跳转锚点，内置五级降级定位链与置信护栏）、③ 完整封标交付包 `.zip`（含偏离表与《人工终核签字确认单.docx》）；支持管理员带条件导出 UTF-8 BOM 安全审计日志 CSV（防公式注入转义）。 |

---

## 🛡️ 核心工程铁律与技术亮点

1. ⚡ **凡能算的绝不进大模型（0-Token 优先）**：分项乘加、人民币大小写转换、限价比较、日期差、集合覆盖 100% 由确定性 Python 执行；大模型仅处理语义判定且只消费局部滑窗上下文。
2. 💵 **金额全程 `Decimal` 禁 `float`**：统一 `ROUND_HALF_UP` 量化至 `0.01` 元，差 1 分钱必须报警；自动处理纵向合并单元格（按 `cell._tc` 本体去重）与多明细表汇总防重计。
3. 🔒 **强制原文逐字溯源（`quote_verifier`）**：所有大模型摘录均经过 `verify_quote()` 原文连续子串核验，未通过核验一律清空并标记 `quote_verified=False`，杜绝幻觉编造。
4. 🚦 **宁缺毋滥、诚实降级**：报价表结构无法 100% 确定时显式返回 `NEED_MANUAL` 引导人工回填；批注定位采用五级降级链（命中率 `<60%` 或 `<8` 字自动回退首部总览绝不乱锚）；页码无法精确获知时回退字符偏移 `@charN`。
5. 🏢 **企业级数据安全与多租户隔离**：支持部门级多租户严格隔离（跨租户访问一律返回 `404`）、全写操作安全审计留痕；支持接入企业内网私有化模型网关（`vLLM / Ollama / MaaS`），个人 API Key 经服务端派生密钥加密落库，永不回显明文。

---

## 🧠 F7 六大工具与三级混合检索（Rerank）原理

### F7 追问引擎的七大受控工具（`core/bid_react_agent.py`）

工具层为**确定性函数**（老项目独立 `core/tools.py` 注册表模式的内嵌版），LLM 只决定"何时调哪个、怎么解读结果"，工具本身零幻觉、可单测：

| # | 工具 | 职责 | 底层实现 |
|---|---|---|---|
| 1 | `search_tender` | 招标文件关键词检索 → ±200 字原文窗口 + 字符偏移 + 双轨页码 | `_search_text_windows` 确定性定位 |
| 2 | `search_bid` | 投标文件关键词检索（同上） | 同上 |
| 3 | `locate_chapter` | 章节标题定位 | `tender_extractor.locate_chapters` |
| 4 | `recalc_price` | 报价算术复算 → 沙盒 findings | F2 `PriceSandbox`（Decimal，0 Token） |
| 5 | `check_cert_expiry` | 证照效期/串名核验 | F3 `scan_qualifications`（0 Token） |
| 6 | `cite_regulation` | 权威法条检索 → 条文原文 + 法律名 + 条号（库空诚实报"法规库为空"） | E4 法规库（大模型对法条真伪零裁决权） |
| 7 | `search_attachment` | 上传附件（文档/图片）关键词/文本定位 | E24 附件检索（带 `INVALID_ATTACHMENT_ID` 守卫与溯源） |

ReAct 循环支持 1~8 轮（默认 7 轮，覆盖 6 工具+1 收口）；`final` 回答中的引文强制过 `verify_quote()` 原文逐字核验，杜绝编造；无模型网关时自动降级 `keyword_fallback` 确定性检索；支持单轮携带图片经 VLM 多模态识图问答与单实例拒图熔断。

### 三级自适应混合检索与 Rerank 原理（`core/hybrid_retriever.py`，E2 交付 / E12 激活）

- **触发门控**：85%+ 规范条款在 F4 初筛（条款号/标题/2-gram 重合度）0ms 直通，仅"疑似漏应答"复核与 F7 自然语言提问才进入混合检索；
- **Level 1（0 Token，<3ms）**：纯 Python Okapi BM25（k1=1.5, b=0.75）+ **45 组招投标领域同义词**双向扩展（如"质保期"↔"免费维保期"，同组命中映射共享概念 token 并获 2.2 倍加权）+ 中文 2-gram 分词；
- **Level 2 = Rerank 精排**（配置了向量模型时自动激活，`models.yaml` 的 `models.embedding`）：全文按行切块 → embedding 向量化（**SHA256 文件级缓存** `data/cache/{hash}.emb.npy`，同一文档只算一次）→ 余弦相似度粗筛 Top-15 → 与 BM25 归一化得分**加权融合（0.55×语义分 + 0.45×字面分）** → 精排取 Top-3。本质：把"字面匹配度"与"语义相似度"两个维度融合后重新排序；
- **Level 3 兜底**：无向量模型 / 切换无 embedding 的内网网关 / `BIDGUARD_FORCE_ZERO_TOKEN=1` / 任何 embedding 异常（404、超时等）→ **静默无感知回退 Level 1**，零报错、绝不中断体检与追问。

---

## 🏗️ 系统架构与目录结构

### 4.1 五层技术栈

```text
⑤ 应用展现层    Vue 3 (<script setup lang="ts">) + Vite 5 + Element Plus (按需引入) + Pinia + ECharts
                 12 个业务视图组件，支持「工业浅岩灰 / 曜石炭褐 / 深海莫兰迪」三套侧栏主题切换
④ 异步服务层    FastAPI + Pydantic v2 + Uvicorn + SSE 实时进度流 (fetch + ReadableStream 指数退避重连)
                 bcrypt 密码哈希、HMAC-SHA256 令牌、多租户隔离、写操作审计留痕、11 组 REST 路由
③ 智能编排层    0-Token 确定性引擎 (F1 正则 / F2 Decimal 算术沙盒 / F3 证照串名 / bid_aligner 四阶段对齐)
                 + 受控 LLM 分段核验 (F1 结构化 / F4 响应核验 / F6 控标雷达 / F7 ReAct 6 工具追问)
                 + BM25/同义词/向量三级门控混合检索 + quote_verifier 逐字溯源 + E4 法条三态核验
② 文档解析层    python-docx (Word 解析 / vMerge 去重 / OOXML 批注 / 双轨页码)
                 PyMuPDF (PDF 解析 / 页码映射 / A4 总览页 + LINK_GOTO 跳转高亮) + RapidOCR 离线缓存
① 数据存储层    PostgreSQL 16 主库（开发/单测无缝回退 SQLite，11 张表）+ Redis 7 + 本地法规与真语料底座
```

### 4.2 仓库目录结构

```text
BidGuard-Agent/
├── README.md                            # 项目总览与快速启动说明（本文档）
├── HANDOVER.md                          # 架构与交接活文档（模块状态、三本账纪律、演进记录）
├── AGENTS.md                            # 工程红线与协作纪律
├── INTERFACE.md                         # core ↔ backend 接口签名与 11 张数据库表契约
├── FRONTEND.md                          # 前端视图、路由与状态管理架构说明
├── docker-compose.yml / Dockerfile      # Docker 四容器生产编排（nginx + app + postgres + redis）
├── requirements.txt                     # Python 依赖清单（版本已全量钉死）
├── 启动BidGuard.bat                     # Windows 一键启动脚本
├── config/
│   ├── models.yaml                      # 模型网关配置（demo=智谱开放平台 / prod=内网私有化端点）
│   └── regulation_sources.yaml          # 招投标权威法规官方源配置
├── core/                                # 核心算法与智能体引擎（19 个模块，零 Web 框架耦合）
│   ├── parser.py                        # Word/PDF/TXT 解析、vMerge 去重、双轨页码、RapidOCR 缓存
│   ├── tender_extractor.py / tender_llm.py  # F1 招标红线、必审清单与所需证照提取（确定性 + LLM 层）
│   ├── price_extractor.py / price_sandbox.py # F2 报价表结构提取与 Decimal 算术核验沙盒
│   ├── qualification_scanner.py         # F3 证照效期/串名扫描 + E26 企业证照台账四态对账
│   ├── bid_aligner.py                   # 双书四阶段条款对齐底座与五类关键要素暗改探针
│   ├── hybrid_retriever.py              # E2 三级门控混合检索（BM25 + 45 组招投标同义词 + 向量）
│   ├── response_checker.py              # F4 ★/▲ 逐条响应勾稽、截图探针与两阶段并发核验
│   ├── deviation_exporter.py            # F5 标准六列《技术与商务偏离表.docx》生成器
│   ├── rigging_radar.py                 # F6 投前排他性/倾向性控标参数雷达（支持单条重扫）
│   ├── bid_react_agent.py               # F7 标书专家多轮 ReAct 追问智能体（7 大工具 + 多文件/图片问答）
│   ├── regulation_library.py / regulation_crawler.py # E4 法规库三态真伪核验、单条维护与官网爬虫
│   ├── redline.py                       # Word OOXML 批注气泡 + PDF 总览跳转页与五级定位高亮
│   └── quote_verifier.py / llm_gateway.py # 原文逐字溯源守门员与多环境 OpenAI 兼容网关（含 VLM 缩图熔断）
├── backend/                             # FastAPI 服务端（main.py / models.py 11表 / worker.py / 11 组路由）
├── frontend/                            # Vue 3 + TypeScript + Vite 前端工程
├── data/
│   ├── corpus/m0_samples/               # 13 份真实政采招标书语料 + baseline / ground_truth / llm_eval 三本账
│   ├── corpus/sim_bids/                 # 10 万字级高仿真投标书语料
│   └── corpus/regulations/              # 招投标法律法规 JSON 种子与官方原文（8 部法律）
├── tests/                               # 31 个测试文件、227 项全链路自动化测试
└── docs/
    ├── DEPLOY.md                        # Docker 四容器部署、备份与故障排查手册
    └── cards/                           # 历次迭代任务卡归档（B1 ~ E26，共 33 份）
```

---

## 🚀 快速启动与运维指南

系统支持 **「Docker 四容器生产模式（PostgreSQL 16）」** 与 **「本机轻量直跑模式（SQLite 零外部依赖回退）」** 两种方式。

### 方式 A · 本机开发/演示直跑（Windows PowerShell，零数据库依赖）

1. **准备 Python 虚拟环境与依赖**（若项目已带 `.venv` 可直接跳过安装）：
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\pip install -r requirements.txt
   ```

2. **编译前端静态产物**（或开发期单独启动 Vite 热更新服务）：
   ```powershell
   cd frontend
   npm install
   npm run build
   cd ..
   ```

3. **配置环境变量（可选）**：
   ```powershell
   Copy-Item .env.example .env
   # 可在 .env 中填入 ZHIPU_API_KEY；若留空，系统也能以 0-Token 纯确定性模式完整运行，
   # 或在登录系统后进入「私有模型服务配置」页面在线绑定个人 API Key。
   ```

4. **启动后端服务**（或直接双击根目录 `启动BidGuard.bat`）：
   ```powershell
   Remove-Item Env:\DATABASE_URL -ErrorAction SilentlyContinue
   .\.venv\Scripts\python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --env-file .env
   ```
   > 若需前端热重载开发，可另开终端执行 `npm --prefix frontend run dev`（访问 `http://127.0.0.1:5173`，API 自动代理至 `8000` 端口）。

### 方式 B · Docker Compose 四容器生产部署（推荐）

包含 `bidguard-nginx`（对外 80 反代）、`bidguard-app`、`bidguard-postgres`（PostgreSQL 16）、`bidguard-redis`（Redis 7）：

```powershell
# 1. 构建前端生产包
npm --prefix frontend run build

# 2. 生成环境配置文件
Copy-Item deploy/.env.docker.example .env

# 3. 一键构建并启动四容器（构建前请关闭本机系统代理/TUN 模式以免干扰镜像源拉取）
docker compose up -d --build

# 4. 检查四容器 Healthy 状态
docker compose ps
```

### 方式 C · Linux 服务器部署（生产推荐）

> 前置：Ubuntu 22.04 / Debian 12 等主流发行版，具备 sudo 权限；防火墙/云安全组放行 `APP_PORT`（默认 8000 或 80）。

```bash
# 1. 安装 Docker Engine 与 Compose 插件（国内服务器建议同时配置镜像加速）
curl -fsSL https://get.docker.com | bash
sudo systemctl enable --now docker

# 2. 获取代码与配置（首次部署一次性执行）
git clone <仓库地址> BidGuard-Agent && cd BidGuard-Agent
cp deploy/.env.docker.example .env
nano .env   # 必改：POSTGRES_PASSWORD、AUTH_SECRET、ADMIN_INITIAL_PASSWORD、ZHIPU_API_KEY；生产环境设 SEED_DEMO_DATA=0、ENABLE_API_DOCS=0

# 3. 构建并启动四容器（构建前确认无全局代理/TUN 劫持，避免镜像源拉取受阻）
docker compose up -d --build

# 4. 健康检查：四容器全 healthy 即就绪，浏览器访问 http://<服务器IP>:8000
docker compose ps
curl -s http://127.0.0.1:8000/api/health

# 5. 日常运维命令
docker compose logs -f app          # 跟踪应用日志
docker compose exec postgres pg_dump -U bidguard bidguard > backup_$(date +%F).sql   # 备份主库
docker compose up -d --build        # 代码更新后重建（requirements 未动时秒级）
```

> **注意**：数据卷（`pgdata` 等）随 `docker compose down` 保留；**切勿**在数据卷目录执行 `docker compose down -v`（将清库）。

### ⚙️ 环境变量速查表

全量变量字典见 [`.env.example`](file:///e:/agent/BidGuard-Agent/.env.example)（本机直跑）与 [`deploy/.env.docker.example`](file:///e:/agent/BidGuard-Agent/deploy/.env.docker.example)（容器编排），每个变量均带用途注释。部署最常改的几个：

| 变量 | 说明 | 何时必改 |
| :--- | :--- | :--- |
| `AUTH_SECRET` | 令牌签名密钥（HMAC-SHA256） | 生产/公网必改（≥32 位随机串，配合 `DEBUG=0` 启动守卫强制校验） |
| `ADMIN_INITIAL_PASSWORD` | 首次建库时 admin 初始密码（仅空库生效） | 生产必改；之后在页面内改密 |
| `ZHIPU_API_KEY` / `MODEL_ENV` | 大模型网关档位与 Key | 不填则全系统以 0-Token 确定性模式运行；也可登录后网页内按用户绑定 |
| `APP_PORT` | Nginx 对外映射端口（默认 8000） | 端口冲突时改道（如 80）；改完 `docker compose up -d` |
| `SEED_DEMO_DATA` | 空库首次启动灌入演示数据 | 生产置 `0` |
| `ENABLE_API_DOCS` | `/docs`、`/redoc`、`/openapi.json` 自动文档公开开关 | 生产置 `0`（防接口枚举侦察）；关闭不影响任何业务功能 |
| `DEBUG` | 调试模式 | 公网部署必须 `0`（否则拒绝以默认密钥启动） |
| `DATABASE_URL` | 数据库连接串 | 本机直跑默认 SQLite；容器模式由 compose 自动注入，无需手填 |

---

## 👤 预置演示账号与在线模型配置

启动成功后，浏览器访问 `http://127.0.0.1:8000`（Docker 部署访问对应端口）即可使用以下预置账号登录：

| 角色 | 用户名 | 默认密码 | 权限说明 |
| :--- | :--- | :--- | :--- |
| **系统管理员** | `admin` | `BidGuard2026!` | 全功能权限 + 招投标法规库导入/删除/官网同步 + 安全审计留痕日志查看 |
| **投标商务经理** | `liming` | `BidGuard2026!` | 政企系统集成投标部成员，可执行 F1~F7 全部标书体检、偏离表生成与个人模型配置 |

**在线配置大模型服务**：
登录后点击左侧导航栏 **「私有模型服务配置」**，可一键选择预设模板（企业内网 `vLLM/Ollama`、智谱开放平台、DeepSeek、Kimi、通义千问、SiliconFlow）并填入 API Key，点击「测试连通性」验证通过后即可启用双轨引擎。

---

## 🧪 自动化测试与质量基线

本项目建立了严格的 **131 项全链路自动化测试基线**（涵盖文档解析、F1~F7 各算法内核、混合检索、法规三态核验、双轨页码、五级批注定位、11 张数据库表模型及 11 组 FastAPI 路由端到端多租户隔离测试）。测试默认运行于离线 `0-Token` 确定性模式，不消耗外部 Token：

```powershell
# 运行全量后端与算法内核测试（131 passed）
.\.venv\Scripts\python.exe -m pytest tests/ -q

# 运行前端 TypeScript 类型检查与生产构建
npm --prefix frontend run build
```

---

## 🗺️ 迭代路线图与交付档案

- ✅ **B1** 数据存储层：11 张业务表 ORM、多租户部门隔离、bcrypt 密码哈希与 HMAC 鉴权
- ✅ **B2** 异步服务层：11 组 FastAPI 路由、后台 Worker 线程池与 SSE 实时流水线
- ✅ **C1~C3** 应用展现层：Vue3 + TypeScript + Element Plus 12 视图落地、三色主题切换
- ✅ **D1** 部署交付层：Docker 四容器编排（Nginx 80 入口 + SSE 防缓冲 + 健康探针）
- ✅ **E1** 确定性战力：评分表 100 分守恒校验、跨格预算提取、Word 内嵌图片离线 OCR
- ✅ **E2** 混合检索增强：BM25 + 45 组招投标领域同义词词典 + 向量语义检索 + `MISSING_EVIDENCE` 截图探针
- ✅ **E3/E3R** 交互体验升级：双书下载、大盘倒计时跳转、F3 证照正则精修
- ✅ **E4** 法规知识库：8 部招投标核心法律底表、法条三态核验（exact_match/fuzzy_hint/unverifiable）与第 6 工具 `cite_regulation`
- ✅ **E5** 批注能力越级：Word/PDF 原位标红批注、A4 首部总览页 + `LINK_GOTO` 点击跳转、五级降级定位链与置信护栏
- ✅ **E6** 双轨页码体系：Word 印刷页码 / 物理页估算 / `@字符偏移` 诚实回退全链路
- ✅ **E7** 报价提取鲁棒性：限价语境逐格剔除、同行多金额歧义护栏（`price_cap` 命中提升至 11/11）
- ✅ **E8** 多明细表小计聚合：分项汇总表权威源聚合与整表纵向合并去重
- ✅ **E10** 交互走查与并发稳定性：全页面历史列表/回放/二次确认删除闭环、长文本对齐 50× 加速防 CPU 空转、SSE 退避重连与 Toast 去重

---

## 📂 核心工程文档索引

| 文档 | 内容说明 | 维护纪律 |
| :--- | :--- | :--- |
| [`HANDOVER.md`](file:///e:/agent/BidGuard-Agent/HANDOVER.md) | **必读当前交接主文档**：基线、19 个核心模块状态、11 份政采真语料三本账、阶段交付记录 | 每阶段/每张卡完成后立即同步 |
| [`AGENTS.md`](file:///e:/agent/BidGuard-Agent/AGENTS.md) | **工程红线与协作纪律**：功能铁律（0-Token优先/Decimal禁float/逐字溯源）；**冷启动口令亦置于文件顶部** | 架构规则沉淀时立即同步 |
| [`INTERFACE.md`](file:///e:/agent/BidGuard-Agent/INTERFACE.md) | **接口契约锁死**：`core ↔ backend` 核心函数签名、11 张数据库表字段规格 | 涉签名与表结构变动时同步 |
| [`FRONTEND.md`](file:///e:/agent/BidGuard-Agent/FRONTEND.md) | **前端工程规范**：12 个视图组件说明、Element Plus 深路径按需引入、状态管理 | 前端架构调整时同步 |
| [`docs/DEPLOY.md`](file:///e:/agent/BidGuard-Agent/docs/DEPLOY.md) | **部署与运维手册**：Docker 四容器生产编排、数据卷备份恢复、常见故障排查 | 部署配置调整时同步 |
| [`docs/cards/`](file:///e:/agent/BidGuard-Agent/docs/cards/) | **任务卡归档**：B1 ~ E10 全部技术卡设计与验收记录 | 每卡归档 |

---

## 🤝 协作模式

本项目采用 **AI 辅助深度敏捷工程方法**：
- **架构/验收 AI**：制定工程红线、锁死接口契约、编写独立端到端集成测试、对齐真值验收；
- **实现 AI**：严格按单卡要求实现，不越界改动未授权模块，遵守 0-Token 与 Decimal 铁律；
- **人类工程师**：把控核心业务方向、真实政采语料提供与关键方案决策拍板。

---

<p align="center">
  <i>BidGuard-Agent · 卖方标书防废标体检与偏离表生成系统 · 坚守 0 编造、0 漏检、0 虚增工程底线</i>
</p>
