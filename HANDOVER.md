# HANDOVER · BidGuard-Agent 交接主文档（V2 活文档）

> 本文件是 `BidGuard-Agent`（投标方标书防废标体检与偏离表生成智能体）唯一主交接文档：
> 任何 AI 或开发人员零上下文进场时，**必须先读本文档 + `AGENTS.md`（工程红线）**，再按任务卡读取
> `需求文档-标书防废标体检智能体PRD.md`、`data/corpus/m0_samples/ground_truth/_README.md` 及对应源码。
>
> **同步纪律（2026-11-07 用户定）**：每完成一个阶段/一张卡，本文档与 `docs/cards/` 必须同步更新，
> 不允许出现"文档停在若干阶段前"的冻结快照状态。
>
> **冷启动口令**（新会话/新 AI 进场标准开场）：
> 「先读取 AGENTS.md 和 HANDOVER.md，然后读 docs/cards/ 里最新一张卡，汇报理解后等我派活。」

---

## 1. 项目定位与技术栈

### 1.1 一句话定位
卖方投标团队的**"封标前最后一道安全锁 + 投前控标雷达"**：上传甲方招标文件与我方投标书正本，几分钟内完成全量废标项体检（资格/符合性/★▲强制条款/报价算术/证照效期/串名穿帮/前后承诺矛盾）、在投标书原位标红批注（附 PDF 总览跳转页），并一键生成填好真实页码的《技术与商务偏离表.docx》；同时支持立项前扫描招标书排他性控标参数、法规库法条三态核验。**严守卖方防废标视角，不碰买方采购寻源。**

### 1.2 五层技术栈（整体继承 Contract-Review-Agent 成熟生产栈）

```text
⑤ 应用展现层    Vue 3 (<script setup lang="ts">) + Vite + Element Plus (深路径按需引入) + ECharts
                 默认工业浅岩灰侧栏（支持浅岩灰/深暖炭褐灰/莫兰迪深青灰三色切换），12 个视图全部落地
④ 异步服务层    FastAPI + Pydantic + Uvicorn + SSE 实时进度流
                 bcrypt 密码哈希、HMAC-SHA256 令牌、多租户部门隔离、写操作审计留痕、
                 Redis 7 / 进程内双模任务队列、API Key 派生密钥加密落库、静态托管 frontend/dist、
                 11 组路由（含 /api/regulations 法规库）
③ 智能编排层    0 Token 确定性引擎（F1正则/F2算术沙盒/F3证照串名/bid_aligner四阶段双书对齐）
                 + 受控 LLM 分段核验（F1分段结构化/F4响应与矛盾核验/F6控标雷达/F7 ReAct追问）
                 + quote_verifier 强制原文逐字溯源锁账
                 + E2 混合检索：BM25 三级门控（词面/同义词45组/语义）+ MISSING_EVIDENCE 探针
                 + E4 法规库：法条三态核验（exact_match/fuzzy_hint/unverifiable，LLM 零裁决权）
① 数据存储层    PostgreSQL 16 主库（开发/演示无缝回退 SQLite，11 张表）、Redis 7 队列与缓存、
                 data/rules/ 招投标法规与废标规则库、data/corpus/ 11份政采真语料三本账 + 法规底表
② 文档解析层    python-docx（Word解析/vMerge去重/OOXML批注/E6 双轨页码）、
                 PyMuPDF（PDF解析/页码换算/坐标高亮/E5 总览页与链接批注）、rapidocr-onnxruntime（扫描件与图片离线OCR缓存）
```

### 1.3 目录结构总览（当前实况，全部已实现）

```text
BidGuard-Agent/
├── HANDOVER.md                          # 本文件：交接主文档（V2 活文档，随阶段滚动更新）
├── AGENTS.md                            # 工程红线与协作纪律（实现 AI 必读）
├── 需求文档-标书防废标体检智能体PRD.md      # v1.3：3 周 / 15 工作日排期
├── 流程图-投标标书.html / 投标原型图.html   # 业务流程图与前端原型
├── README.md / INTERFACE.md / FRONTEND.md / requirements.txt / .env.example
├── docker-compose.yml                   # 四容器编排（nginx:alpine + app + postgres:16 + redis:7）
├── Dockerfile                           # Python 3.12-slim（digest 钉死）+ aliyun 源 + rapidocr 离线模型
├── config/
│   ├── models.yaml                      # 模型网关 demo=智谱 / prod=内网私有化
│   └── regulation_sources.yaml          # E4：8 部法律官方 URL（crawl 数据源）
├── core/                                # 19 个模块（F1~F7 + 混合检索 + 法规库，逐项状态见 §3）
├── backend/                             # main + models(11表) + schemas + 11 组路由
├── frontend/                            # Vue3 + TS：12 个视图全部落地
├── tests/                               # 24 个测试文件（test_*.py）161 项全绿
├── scripts/                             # build_f1_baseline.py / eval_f1_llm.py
├── data/
│   ├── corpus/m0_samples/               # 11 份语料 + baseline / ground_truth / llm_eval 三本账（§4）
│   ├── corpus/regulations/              # E4：4 部法律 JSON 种子 + originals/ 原文（含 87 号令扫描 PDF）
│   └── rules/law_source/                # 4 部法规官方文本
├── deploy/nginx/nginx.conf              # 静态直发 + /api/ 反代 SSE 防缓冲三件套
└── docs/                                # DEPLOY.md / cards/（任务卡归档 B1~E19，共 26 份）/ 待立卡-E1E2升级点定案.md
```

---

## 2. 当前基线（截至 2026-11-10）

- **开发阶段**：3 周排期全部完成 + 十九轮增强升级（E1~E19）全量交付，仿真标书 **11/11 验收收官**，前后端体验与 F4 并发提速闭环完成
- **Git 最新功能 Commit**：`b8f3403`（含后续 F4 降级暴露补强；远端同步状态以 `git log origin/main..HEAD` 实数为准）
- **测试基线**：**161 项 pytest 全绿（零回归）** + `vue-tsc --noEmit` 零错误 —— 第一验收线已从 60 项演进至 161 项
- **模型网关基线**：`config/models.yaml` 默认 `default_env: demo`（智谱 `glm-5.3-flash` 直连，`reasoning_effort: low`，`embedding: embedding-3` 已激活 L2 混合检索）；预留 `prod` 内网私有网关（`http://10.0.2.81:3000/v1`，无向量模型自动无感降级 Level 1）
- **运行环境**：本机开发用项目自带 `.venv`（依赖已全量钉死）；生产走 Docker 四容器（`docker compose up -d --build`，层缓存全热时秒级）
- **快速验收命令**：

```powershell
.venv\Scripts\python -m pytest tests/ -q
# LLM 相关测试为 Mock 离线（0 Token）；真调对账需在 .env 配好 ZHIPU_API_KEY 后运行 scripts/eval_f1_llm.py
```

---

## 3. 核心模块实现清单（`core/` 下 19 个文件逐项状态）

| 序号 | 文件路径 | 所属模块 | 一句话职责说明 | 当前状态与实测验收数据 |
|---|---|---|---|---|
| 1 | `core/interfaces.py` | 基础契约 | `ParsedDoc`、`Clause`、`LLMResponse` 等核心数据类 | 已验收（全模块共用契约） |
| 2 | `core/parser.py` | 文档解析 | Word/PDF/TXT 解析、vMerge 去重、RapidOCR 离线缓存；E5 新增 `build_pdf_page_spans/offset_to_page`；E6 新增 `build_docx_page_spans/offset_to_docx_page`（双轨页码：印刷页 > 物理页估算 > 字符偏移诚实回退）。**E6 验收时偏移权威源已从 lxml 重建改为 python-docx 部件序列（7/7 真语料零漂移）** | 已验收（11 份真语料解析零虚增） |
| 3 | `core/splitter.py` | 条款切分 | 条款编号与段落切分，保留精确 `start/end` 偏移 | 已验收 |
| 4 | `core/quote_verifier.py` | 溯源锁账 | `normalize_zh` 中文归一 + `verify_quote` 连续原文子串核验 | 已验收（F1/F4 防幻觉守门员） |
| 5 | `core/llm_gateway.py` | 模型网关 | OpenAI 兼容多环境调用、线程安全用量累加（`self._lock`）、`embed()` 向量接入、原生 `reasoning_effort` 透传与拒参自动重试 | 已验收（E12 激活 `embed()`；F1 实测 178 次调用 0.231 元） |
| 6 | `core/tender_extractor.py` | **F1 确定性层** | ★/▲ 分型合并、无标记模式 A/B、资格/符合/评标章定位、表格型评分细则解析（0 Token）；E18 接入统一双轨页码上下文 | **已验收**（11 份语料逐字符守恒，docx 页码全非空） |
| 7 | `core/tender_llm.py` | **F1 LLM 层** | 候选段扩窗吸附、逐段结构化、强制溯源、段落型评分细则抽取 | **已验收**（召回率/溯源率 100%） |
| 8 | `core/price_sandbox.py` | **F2 算术沙盒** | 0 Token 报价核验（全程 Decimal 禁 float）、大写转换、限价比较 | **已验收**（差 1 分钱必报警） |
| 9 | `core/price_extractor.py` | **F2-2 提取层** | 报价表提取、vMerge 去重、万元折算、NEED_MANUAL 降级；E7~E9/E13/E14 迭代覆盖同行多金额护栏、多明细表聚合、元/年×期限、双胞胎去重、汇总行后缀识别与明细豁免 | **已验收**（`price_cap` 命中 **11/11**；芜湖无为 0 error） |
| 10 | `core/qualification_scanner.py` | **F3 证照串名** | 证照效期、社保月份区间、跨项目串名、占位符扫描（0 Token） | **已验收**（E3R 修复社保月份正则；E15 补双轨页码与开标日自动提取） |
| 11 | `core/bid_aligner.py` | 对齐底座 | 双书条款级四阶段 + Stage 3.5 跨块回收对齐、五类要素异动探针 | 已验收（0 Token 底座；E10 防呆优化提速 50×） |
| 12 | `core/response_checker.py` | **F4 双书勾稽** | ★/▲ 逐条响应核验 + 大模型判定 + 自洽扫描；E6 接入 docx 双轨页码；E5 类名符号动态化；E19 升级“阶段 A 按序预定位 + 阶段 B 仅 `client.chat` 并发”双阶段引擎 | **已验收**（E19 `BIDGUARD_F4_CONCURRENCY` 默认 4，与串行逐字节一致） |
| 13 | `core/deviation_exporter.py` | **F5 偏离表** | 《技术与商务偏离表.docx》一键生成；E6 位置列升级为"第X页(印刷页Y·估算)"带 @charN 诚实回退 | **已验收** |
| 14 | `core/rigging_radar.py` | **F6 控标雷达** | 排他性/倾向性控标参数识别（确定性正则 + 大模型④，标注"疑似"）；E18 接入统一双轨页码 | **已验收**（E3R 修复 import；E18 打通 docx 页码） |
| 15 | `core/bid_react_agent.py` | **F7 专家追问** | 多轮 ReAct 追问；E4 挂载 `cite_regulation` 第 6 工具；E18 兼容双轨页码与 `list \| dict` 传参；E19 支持三挡 `reasoning_effort`（low/medium/high）透传 | **已验收** |
| 16 | `core/redline.py` | 批注回写 | Word OOXML 批注气泡 + PDF 坐标高亮；**E5 升级**：五级降级定位链（L3 标点归一/L4 逐字符 bbox 归一/L5 置信护栏≥60%且≥8字）、总览页（A4 插入 PDF 首部，全部发现编号 N01.. + LINK_GOTO 点击跳转） | **已验收**（E5 卡，111→115 项含 6 新测试） |
| 17 | `core/hybrid_retriever.py` | **E2 混合检索** | BM25 三级门控（词面精确/同义词扩展 45 组/语义兜底）+ MISSING_EVIDENCE 探针；E12 正式激活 L2 向量嵌入重排（带 `.emb.npy` 维度校验失效重算） | **已验收**（E2 + E12 卡） |
| 18 | `core/regulation_library.py` | **E4 法规库** | 8 部法律条文库：三态核验（exact_match/fuzzy_hint/unverifiable，**LLM 对真伪零裁决权**）、中文/阿拉伯条号归一、张冠李戴降级 fuzzy_hint、四格式导入（json/txt/docx/pdf，content_hash 幂等） | **已验收**（E4 卡，含 crawl 集成测试外网隔离） |
| 19 | `core/regulation_crawler.py` | **E4 法规爬取** | 静态 HTML 适配器，运行时读 `config/regulation_sources.yaml`（8 个官方 URL）；占位源诚实报"源未配置"；超时重试一次转 manual_required；SHA256 条文指纹发现官网修订 | **已验收** |

---

## 4. 数据资产与"三本账"读写纪律

### 4.1 11 份政采真实招标书语料（`data/corpus/m0_samples/`）
8 份 `.docx` + 3 份 `.pdf`（亳州学院、江苏科技一件事、江苏固废、南京医科大四院病理、无锡梁溪档案、无锡新吴自然资源、徐州智慧文旅、连云港数智公积金、芜湖无为档案、滁州物业维修资金、滁州城职院数智托育），3 份 PDF 均为文本型非扫描件。公告日期与官方 URL 已在 `语料清单.md` 100% 登记。

### 4.2 三本账目录职责与严禁混写铁律

| 账本目录 | 性质 | 读写权限铁律 |
|---|---|---|
| `baseline/` | 机器确定性候选清单（`scripts/build_f1_baseline.py` 产物） | **可随时由脚本重跑覆盖** |
| `ground_truth/` | 人工三轮交叉复核定稿的 F1 验收真值 | **只读圣域：任何自动化脚本严禁写入** |
| `llm_eval/` | F1 LLM 结构化产物 + 评测报告 | 已入库 Git，作为 F4/F5 离线零成本测试桩 |

**提取器升级标准流程**：改正则 → 重跑 `baseline/` → 与 `ground_truth/` 人工 diff → 确认后手动同步 `ground_truth/` 并在 `_README.md` 登记改判原因。

### 4.3 `kind` 三分类消费契约（下游必读）
- `requirement`：实质要求（`structured.consequence` 区分 veto/deduction/bonus/none）→ F1/F4/F5 正常消费；
- `usage_note`：标记使用说明 → F4/F5 必须排除；
- `table_marker`：附表行标记碎片 → F4/F5 必须排除。

### 4.4 法规底座（E4 起新增）
- `data/rules/law_source/`：4 部法规官方文本（87 号令、政府采购法、实施条例、招标投标法）；
- `data/corpus/regulations/`：4 部法律 JSON 种子（招标投标法 68 条等）+ `originals/` 用户提供的原文（含 87 号令 6.4MB 扫描版 PDF）；
- `config/regulation_sources.yaml`：8 个官方 URL 已填齐（commit `fb06a50`）。**注意**：种子中反不正当竞争法（2019）与中小企业支付条例（2020）已过时，待 crawl 更新到 2025 修订版；招标投标法实施条例与 74 号令库内本无，待 crawl 新增。

---

## 5. 已知局限与遗留待办（Backlog）

1. **F1 段落型评分细则抽取精修**：段落型语料存在输出粒度方差与局部跨窗超计（梁溪 27 项/19 项波动、亳州 115 分、滁州物业 240 分）。待立卡：分值合计≠100 自动告警重抽、固定 temperature=0、表格型与正文价格分自动拼合。
2. ~~F2-2 剩余 2 份语料的 `price_cap` 特殊措辞适配~~ **已解决（E7 顺带修复）**：E7 调用点分层改造后 `price_cap` 命中提升为 **11/11**（江苏科技 390,000 与江苏固废 3,700,000 均正确命中，已对照独立来源核实）。
3. **两处语法不可分的人工改判保护**：芜湖 `▲中共无`、南京病理 `★（二）商务要求`——提取器保守不硬判，靠 ground_truth 人工锁死（M0 人工终审红线）。
4. **E4 法规 crawl 实弹验证未跑**：8 个官方 URL 已配置但尚未真实爬取（重点看反法 2019→2025 diff）；用户手工验证 URL 可达性亦未完成。
5. **~~仿真投标语料制作中~~ 已收官（11/11 全验收放行）**：见 `data/corpus/sim_bids/_README.md`——含连云港（62▲/56.8万字）、滁州物业（E13 修复后放行）、芜湖无为（E14 修复后 0 error/0 warning 收官）在内的 11 份仿真投标书全部验收通过，全套语料零返工。
6. **F7 问答工作台布局增强（待办池，未立卡）**：`QaChatView.vue` 左侧会话列表折叠抽屉、历史搜索与重命名（E19 为防止单卡摊薄而刻意拆出）。
7. **F2-2 / F2 阶段七/八已交付**：E7（`891b3ab`）、E8（`d56a9af`）、E9（`2137047`）、E13（`38bb1d1`）、E14（`e8d1465`）均已验收入库，11 份仿真语料相关算术盲区已清零。

---

## 6. 全栈交付记录（阶段 × Commit，替代旧"待开发规划"）

| 阶段 | 内容 | 关键 Commit | 验收线 |
|---|---|---|---|
| M0+内核 | 语料三本账 + F1/F2/F2-2/F3/F4 | `98d4928` 止 | 60 项测试 |
| B1 | 数据层：9 表 ORM + 鉴权 | B1 卡 | pytest 全绿 |
| B2 | 路由层：10 组 API + SSE 流水线 | B2 卡 | pytest 全绿 |
| C1~C3 | 前端 8 视图 + 三色主题 | C3 收尾卡 | vue-tsc + build |
| D1 | Docker 四容器封版（Dockerfile/compose/nginx/DEPLOY） | `b0745a3` | 四容器 healthy |
| E1 | 确定性战力：评分表 100 分守恒、跨格预算、内嵌图 OCR | `632e58d` | 91 项 |
| E2 | 混合检索 + MISSING_EVIDENCE 探针 | `e32bfdf` | 96 项 |
| E3/E3R | 体验修复 6 项 + Gemini 交叉复查 3 实锤 | `d50c8ef` | 99 项 |
| E4 | 法规库：2 表 + 三端点 + cite_regulation 第 6 工具 + 前端第三页签 | `1cf95bf` | 105 项 |
| E5 | PDF 总览页 + LINK_GOTO 跳转 + 五级定位链 + F4 符号动态化 | `15a7ee3` | 111 项 |
| E6 | Word 双轨页码（印刷页/物理页/字符偏移）全链路 | `95237ee` | 115 项 |
| E7 | F2 开标一览表提取鲁棒性（限价剔除/同行多金额护栏/主源优先） | `891b3ab` | 121 项 |
| E8 | F2 多明细表小计聚合与汇总表纵向合并去重 | `d56a9af` | 127 项 |
| E10 | 全页面历史列表/回放/二次确认删除闭环 + 大盘 F1 统计归零 + 长文本对齐 50× 加速防 CPU 空转 + SSE 退避重连 + Toast 去重 | `c2a2d5b` | 128 项 |
| E9 | F2 明细行全量收集（多字量词/子序号/纵向合并下探）+ 元/年×租赁期限定价 + 父子表多重集防重 | `2137047` | 131 项 |
| E12 | L2 混合检索激活（LLMClient.embed() + models.embedding 配置 + 降级守卫）+ Gemini 交叉复查加固（缓存维度失配作废重算/requests timeout） | `f2e1952`→`fca119d` | 146 项 |
| E13 | F2 双胞胎明细表去重（金额多重集主键 + DUPLICATE_DETAIL_TABLE 提示 + 非双胞胎兜底） | `38bb1d1` | 146 项 |
| E11 | 体检任务五阶段耗时统计（started_at/finished_at/duration_json + 老库平滑加列 + 前端最慢阶段高亮） | `b62887a` | 149 项 |
| E14 | F2 汇总行后缀识别 + 前后缀明细算术豁免护栏 + 页/条/卷量词扩充（Gemini 首版→Antigravity 返工一次过） | `e8d1465` | 151 项 |
| E15 | F3/自定义红线双轨页码 + F2 三级防重名锚定页码 + 开标日期自动回填（消 2026-11-05 硬编码） | `f1c77ec` | 155 项 |
| E16 | 交付中心四大件独立下载 + signoff 端点 + 批量打包内容自选（含 TS 强类型修复 `e08c42b`） | `bdb36f7`→`e08c42b` | 155 项 |
| E18 | F1/F6/F7 三处 PDF 硬编码统一接入双轨页码上下文（docx 页码从恒 None 到全非空，F7 形参 list\|dict 兼容） | `87bde93` | 157 项 |
| E19 | 六子项合并单卡三 Commit：F1 本地时区/多文件拖拽/局部 loading/超时放宽 + 在途秒表（UTC 补 `Z`）+ F7 三挡推理深度 + SSE 断线轮询对齐 + F4 两阶段并发提速（默认 4 路；含 `worker` 对单条 `error_fallback` 暴露 `LLM_FALLBACK` 提示级发现） | `85f0ddd` / `f64b3b6` / `b8f3403` 后续 | 161 项 + `vue-tsc` |
| chore | requirements 全量钉死（`59e6b64`）、基础镜像 digest 钉死、弹窗会话记忆 | `90a1947` | — |

---

## 7. 协作纪律与工程红线（完整版见 `AGENTS.md`）

1. **一卡一验、全绿入库**：每张卡跑通全量 pytest 零回归方可 commit；当前第一验收线 **161 项**。
2. **凡能算的绝不进大模型（0 Token 优先）**；金额全程 `Decimal` 禁 `float`（ROUND_HALF_UP 量化 0.01）。
3. **LLM 只吃局部窗口 + 强制 `quote_verifier` 溯源**；严禁整本标书直塞。
4. **宁缺毋滥显式降级**：无法对齐必返回 NEED_MANUAL；找不到位置绝不乱锚（E5 五级定位链 + 置信护栏）；页码诚实回退字符偏移并标"不一定准确"。
5. **账本隔离**：ground_truth 只读；docx 表格按 `cell._tc` 本体去重。
6. **LLM 对法条真伪零裁决权**（E4）：三态核验纯确定性底表对账。
7. **跨平台卫生**：统一 utf-8 + newline='\n'；库模块静默；PowerShell 5.1 不支持 `&&`。
8. **状态断言必实数**（2026-11-07 新增）："N 个 commit 未推送""剩余 X 项"这类状态必须当场跑命令核实，严禁凭记忆转述。
9. **产出物入盘必机检**（2026-11-07 新增）：Gemini/外部 AI 产出粘贴落盘前先机检 `](http`、`\vert{}`、markdown 围栏、行数对比，粘错文件整文件回退基线再补增量。

---

## 8. 文档体系与冷启动流程

| 文档 | 职责 | 更新纪律 |
|---|---|---|
| `HANDOVER.md`（本文件） | 交接主文档：基线、模块清单、交付记录、Backlog | **每阶段/每卡完成后立即同步** |
| `AGENTS.md` | 工程红线、协作纪律、冷启动口令 | 新铁律沉淀时立即更新 |
| `INTERFACE.md` | core↔backend 契约锁死 | 涉签名改动时同步 |
| `FRONTEND.md` | 前端架构说明 | 前端结构变化时同步 |
| `docs/DEPLOY.md` | 本机直跑 + Linux Docker/离线/裸机部署手册 | 部署变化时同步 |
| `docs/cards/` | 任务卡归档（B1~E19） | 每立卡/收卡时归档 |
| `.workbuddy/memory/`（不入库） | 架构侧 AI 工作日志 | 每日实质工作后追加 |

**冷启动口令**（新会话开场）：「先读取 AGENTS.md 和 HANDOVER.md，然后读 docs/cards/ 里最新一张卡，汇报理解后等我派活。」

---

## 9. 本文档信息来源

1. `docs/cards/` B1~E19 全部任务卡与验收记录；`派发归档/` 各阶段派发包全文
2. Git 提交链（`git log --oneline`，功能代码至 E19）；161 项 pytest + `vue-tsc` 实测回执
3. `data/corpus/m0_samples/` 三本账 + `语料清单.md`、`data/corpus/sim_bids/_README.md`；`data/corpus/regulations/` 法规种子
4. `E:\agent\Contract-Review-Agent\HANDOVER_V2.md`（文档骨架参照）
