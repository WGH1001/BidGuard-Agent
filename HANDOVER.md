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
├── tests/                               # 32 个测试文件（test_*.py）237 项全绿
├── scripts/                             # build_f1_baseline.py / eval_f1_llm.py / probe_reasoning_efforts.py
├── data/
│   ├── corpus/m0_samples/               # 11 份语料 + baseline / ground_truth / llm_eval 三本账（§4）
│   ├── corpus/regulations/              # E4：4 部法律 JSON 种子 + originals/ 原文（含 87 号令扫描 PDF）
│   └── rules/law_source/                # 4 部法规官方文本
├── deploy/nginx/nginx.conf              # 静态直发 + /api/ 反代 SSE 防缓冲三件套
└── docs/                                # DEPLOY.md / cards/（任务卡归档含 B1~E27，共 34 份）/ 待立卡-E1E2升级点定案.md
```

---

## 2. 当前基线（截至 2026-11-12）

- **开发阶段**：3 周排期全部完成 + 二十七轮增强升级（E1~E27）全量实现（E1~E26 已验收并入库；**E27 已完成全部实现与本地验证，留在工作区待最终门禁放行后确认 commit**：涵盖 F3 社保语境防误拼 + 受控 LLM 鉴别、网关超时与 TUN 诊断/F7 重生成与撤回本轮 + `/ask` 502 隔离、`clause_role` 分层与第四章补齐 + F5 偏离表过滤、分包预算限价精准匹配、Stage 1.5 全图 OCR `f_parse_ms` 拆分及 F2–F4 台账吸顶折叠改版）
- **Git 最新功能 Commit**：工作区 E27 改动待确认 commit；当前仓库最新基线提交 `1087a49`（`docs: 全面同步AGENTS/README/FRONTEND至E26与227项验收基线`），前序功能提交 `4af8c66`（E26）/ `d133a21`（E25）/ `29e5d56` & `a166973`（E24 + E4 爬虫修复）
- **测试基线**：**237 项 pytest 全绿（零回归）**（在 227 项主线基础上新增 `tests/test_e27_real_bid_fixes.py` 10 项回归与北交大 02/04 真语料实弹探针）+ `vue-tsc --noEmit` 零错误 + `vite build` 成功 —— 第一验收线已演进至 **237 项**
- **模型网关基线**：`config/models.yaml` 当前工作区设为 `default_env: prod`（内网私有网关 `http://10.0.2.81:3000/v1`，`GLM-5.3-Flash`，`timeout: 60`）；并行提供 `demo` 环境（智谱开放平台 `glm-5.3-flash`、`embedding: embedding-3` 激活 L2 混合检索、`timeout: 60`），`LLMClient` 统一控重试与 TUN/私有 IP 报错排查提示
- **运行环境**：本机开发用项目自带 `.venv`（依赖已全量钉死）；生产走 Docker 四容器（`docker compose up -d --build`，默认宿主机 `APP_PORT=8000` + `POSTGRES_PORT=15432`，与老项目 `8080` + `5432` 并行共存不冲突）
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
| 2 | `core/parser.py` | 文档解析 | Word/PDF/TXT 解析、vMerge 去重、RapidOCR 离线缓存；E5 新增 `build_pdf_page_spans/offset_to_page`；E6 新增 `build_docx_page_spans/offset_to_docx_page`（双轨页码：印刷页 > 物理页估算 > 字符偏移诚实回退）。**E6 验收时偏移权威源已从 lxml 重建改为 python-docx 部件序列（7/7 真语料零漂移）**；**E24 新增** `parse_qa_attachment` 附件低门槛解析（取消 `OCR_MIN_CHARS=100` 报错，复用 RapidOCR 与 `.ocr.txt` 缓存）；**E27 新增** `parse_document(..., ocr_images=True, progress_cb=None)` 支持 Stage 1.5 全图 OCR 进度回调与分批增量刷盘 | 已验收（11 份真语料解析零虚增） |
| 3 | `core/splitter.py` | 条款切分 | 条款编号与段落切分，保留精确 `start/end` 偏移 | 已验收 |
| 4 | `core/quote_verifier.py` | 溯源锁账 | `normalize_zh` 中文归一 + `verify_quote` 连续原文子串核验 | 已验收（F1/F4 防幻觉守门员） |
| 5 | `core/llm_gateway.py` | 模型网关 | OpenAI 兼容多环境调用、线程安全用量累加（`self._lock`）、`embed()` 向量接入、原生 `reasoning_effort` 透传；**E22 加固**：组合条件拒绝识别（屏蔽无关 400 误判）、单实例内存熔断记忆（不改 `self.chat_options`）、`chat_stream()` 组合去参重试、`reasoning_effort_map` 配置翻译与内部键隔离；**E24 新增**：PyMuPDF `Pixmap` 真实像素内存缩图 `encode_image_for_vlm`、`supports_vision` 透传/图像 token 占位估算与 `_is_vision_reject`/`_mark_vision_unsupported` 拒图熔断剥图重试；**E27 加固** 读取 `timeout`（默认 60.0s）+ OpenAI `max_retries=0` 与私网 IP TUN 排查提示（`_format_gateway_error_hint`） | 已验收（E12 / E22 / E24 / E27 实测全绿） |
| 6 | `core/tender_extractor.py` | **F1 确定性层** | ★/▲ 分型合并、无标记模式 A/B、资格/符合/评标章定位、表格型评分细则解析（0 Token）；E18 接入统一双轨页码上下文；E23 新增通用 `open_date` 提取；**E26 新增** `extract_required_certificates` 0-Token 提取全书所需企业资质/产品认证/人员证书清单并纳入 `build_review_checklist`（附 `verify_quote` 溯源与"无需提供证明材料"免报护栏）；**E27 新增** `locate_chapters` 防无空格粘连、`classify_invalid_clause_role`（`requirement`/`eval_rule`/`submission_rule`）与 `extract_chapter4_procurement_requirements`（无 ★/▲ 招标书从第四章补齐 `technical_or_commercial` 条款） | **已验收**（13 份语料逐字符守恒、开标日全命中） |
| 7 | `core/tender_llm.py` | **F1 LLM 层** | 候选段扩窗吸附、逐段结构化、强制溯源、段落型评分细则抽取；**E23 新增** `extract_open_date_fallback_with_llm`（局部窗开标日兜底 + `verify_quote` 与日期一致性双锁） | **已验收**（召回率/溯源率 100%） |
| 8 | `core/price_sandbox.py` | **F2 算术沙盒** | 0 Token 报价核验（全程 Decimal 禁 float）、大写转换、限价比较 | **已验收**（差 1 分钱必报警） |
| 9 | `core/price_extractor.py` | **F2-2 提取层** | 报价表提取、vMerge 去重、万元折算、NEED_MANUAL 降级；E7~E9/E13/E14 迭代覆盖同行多金额护栏、多明细表聚合、元/年×期限、双胞胎去重、汇总行后缀识别与明细豁免；**E27 新增** `_detect_target_package_no` / `_extract_package_specific_cap`（`package_no` 多分包预算限价精准匹配）并支持复用 Stage 1.5 `parsed_doc` | **已验收**（`price_cap` 命中 **11/11**；芜湖无为 0 error） |
| 10 | `core/qualification_scanner.py` | **F3 证照串名** | 证照效期、社保月份区间、跨项目串名、占位符扫描（0 Token）；E15 补双轨页码与开标日自动提取；**E26 新增** `match_required_certificates_with_ledger` 将招标所需证照与租户企业证照台账做四态保守对账；**E27 升级** `_MONTH_SINGLE_RE` 防日期回溯、新增 `resolve_effective_social_requirement` 动态推导开标日社保窗、局部社保缴费窗隔离防误吸与可选 `client` 受控语义鉴别（过 `verify_quote` 核验） | **已验收**（E3R / E15 / E26 全绿；E27 增补） |
| 11 | `core/bid_aligner.py` | 对齐底座 | 双书条款级四阶段 + Stage 3.5 跨块回收对齐、五类要素异动探针 | 已验收（0 Token 底座；E10 防呆优化提速 50×） |
| 12 | `core/response_checker.py` | **F4 双书勾稽** | ★/▲ 逐条响应核验 + 大模型判定 + 自洽扫描；E6 接入 docx 双轨页码；E5 类名符号动态化；E19 升级“阶段 A 按序预定位 + 阶段 B 仅 `client.chat` 并发”双阶段引擎；**E27 升级** `submission_rule` 降级 `info`+`none`、同句复合质保合并与未填下划线提示、L651 `client.chat` 断连单条降级及支持复用 `tender_parsed_doc`/`bid_parsed_doc` | **已验收**（E19 `BIDGUARD_F4_CONCURRENCY` 默认 4，与串行逐字节一致） |
| 13 | `core/deviation_exporter.py` | **F5 偏离表** | 《技术与商务偏离表.docx》一键生成；E6 位置列升级为"第X页(印刷页Y·估算)"带 @charN 诚实回退；**E27** 传入 `include_submission_rules=False` 过滤纯递交/形式纪律，仅输出实质性技术/商务要求 | **已验收** |
| 14 | `core/rigging_radar.py` | **F6 控标雷达** | 排他性/倾向性控标参数识别（确定性正则 + 大模型④，标注"疑似"）；E18 接入统一双轨页码 | **已验收**（E3R 修复 import；E18 打通 docx 页码） |
| 15 | `core/bid_react_agent.py` | **F7 专家追问** | 多轮 ReAct 追问；E4 挂载 `cite_regulation` 第 6 工具；E18 兼容双轨页码与 `list \| dict` 传参；E19 支持三挡 `reasoning_effort`；E21 注入多轮 `history`（最近 6 轮截断+0-Token 逆序回溯）、智谱合法挡位映射与 `ask_bid_expert_stream` 流式事件引擎；E23 新增 `_coerce_action_input` 自动解包字符串化 JSON 与 `recalc_price` 缺失 `total_price` 明确拦截；**E24 扩至 7 工具**（新增 `search_attachment`）、仅当轮按选附 `image_url`、附件逐字 `verify_quote` 强制溯源与纯图视觉降级、`_parse_llm_json(strict=False)` 容错、禁吐残缺工具 JSON 与 `after_offset` 续检 | **已验收**（E21 / E23 / E24 实测全绿） |
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
| E19 | 六子项合并单卡三 Commit：F1 本地时区/多文件拖拽/局部 loading/超时放宽 + 在途秒表（UTC 补 `Z`）+ F7 三挡推理深度 + SSE 断线轮询对齐 + F4 两阶段并发提速（默认 4 路；含 `worker` 对单条 `error_fallback` 暴露 `LLM_FALLBACK` 提示级发现） | `85f0ddd` / `f64b3b6` / `b8f3403` / `b468c35` | 161 项 + `vue-tsc` |
| E20 | F5 偏离表五维分类筛选与统计卡片联动、F6 去旧文本框+多文件拖拽批量+本地时区、F1 历史单行横向滚动/搜索下拉+5MB `localStorage` LRU 淘汰、`docker-compose.yml` Postgres 宿主机端口变量化（`${POSTGRES_PORT:-15432}:5432` 与老项目双开共存） | `df9f74a` | 166 项 + `vue-tsc` |
| E21 | F7 多轮会话 `history` 注入与 0-Token 历史回溯、智谱 `glm-5.3-flash` 合法三挡映射（`low/high/max` 仅调思考深度，三挡均开放 `max_rounds=7` 支持 6 工具+1 收口）、`ask_bid_expert_stream` + `POST /api/qa/sessions/{id}/ask/stream` ReAct 思考轨迹 SSE 直播与流式打字（含 `getStoredToken` 鉴权/`get_session_factory` 写库/0-Token 回溯 3 项验收修补） | `69ad261` / `4f52878` | 167 项 + `vue-tsc` + `vite build` |
| E22 | LLM 网关思考参数防御加固：`_is_thinking_reject` 组合识别（防 `unsupported image type` 误判）、`chat`/`chat_stream` 共用单实例内存熔断与流式 `stream_options` 组合重试补漏、`chat_options.reasoning_effort_map` 配置映射与内部键隔离、内网手动实弹探针 `scripts/probe_reasoning_efforts.py` | `a8ab61f` | 184 项 |
| E23 | 开标日期全链路 11/11 精准提取（含通用锚点同义词、中文大写日期、正则未命中局部窗 LLM 兜底 + `verify_quote` 双锁、手填强校验 `INVALID_OPEN_DATE`、双向 `OPEN_DATE_MISMATCH` 预警及 `open_date_source` 前后端全链路来源可观测）、F1/F6 开标日透出、大盘倒计时优先有日期项+类型徽标、F5 去全白遮罩、顶栏 44px 瘦身+F7 高屏占比与发送旁深度选择器、`_coerce_action_input` 工具 JSON 解包与 `total_price` 校验 | **已验收**（`59b410e`） | 211 项 + `vue-tsc` + `vite build` |
| E24 | F7 标书专家多文件与图片附件问答双轨落地（`parse_qa_attachment` 取消 100 字门禁、`.ocr.txt` 缓存、20MB/10 个/100MB 配额与 `DELETE` 级联清盘、老库双表自动加列、`supports_vision` 三处设置与网关 PyMuPDF 真实像素缩图/拒图单实例熔断、`search_attachment` 第 7 工具 + `INVALID_ATTACHMENT_ID` 拒收 + `verify_quote` 反幻觉与纯图降级、`_parse_llm_json(strict=False)` 容错/禁吐工具 JSON/`after_offset` 续检、前端上传勾选与视觉参考标记） | **已验收**（`a166973` / `29e5d56`） | 218 项 + `vue-tsc` + `vite build` |
| E25 | 全链路工作流与台账运维补强：F5 下拉防锁死/在途 `409 CHECK_TASK_RUNNING`/重跑清旧偏离表；F6 单条 `rescan` 与全站开标日显化/未识别降级；F7 上下文 `suggested_questions`（含备证清单兜底提示）；法规单条删除并重算主表哈希；审计日志 UTF-8 BOM CSV 导出；企业证照可选 PDF/图片 OCR 预填与原件上传下载解绑 | **已验收（工作区待确认 commit）** | 223 项 + `vue-tsc` + `vite build` |
| E26 | 招标所需证照清单 0-Token 确定性提取与企业台账自动对账：`extract_required_certificates` 提取资格门槛/产品强制认证/评分资质/人员证书（带 `verify_quote` 溯源、"无需提供证明材料"免报护栏与品目清单节能产品证书捕获）并接入 `build_review_checklist`；`match_required_certificates_with_ledger` 按租户 `CompanyCertificate` 台账做四态对账（`valid`/`expiring_soon`/`expired`/`not_in_ledger`，以开标日为优先参照）；F1 前端第 ⑤ 页签与台账快捷补录联动 | **已归档（`4af8c66` / `1087a49`）** | 227 项 + `vue-tsc` + `vite build`
| E27 | 真标书实战排障与交互体验深化：F3 社保动态窗口推导与局部语境防误拼 + 受控 LLM 语义鉴别（强过 `verify_quote`）与按 `code` 专属建议；模型网关可控 `timeout=60` / `max_retries=0` 与 TUN 诊断提示、F4 `check_bid_self_contradictions` L651 断连降级、F7 新增 `DELETE /api/qa/sessions/{id}/last-turn` 撤回本轮 + 重新生成/停止生成 + `/ask` `502 QA_GATEWAY_ERROR` 防污染；`clause_role`（`requirement`/`eval_rule`/`submission_rule`/`technical_or_commercial`）分层与第四章采购需求补齐、同句复合质保合并与未填下划线提示；分包预算限价精准匹配 + Stage 1.5 全图 OCR 独立 `f_parse_ms` 与增量 `.media.ocr.json` 刷盘；F2–F4 台账 `.task-rail` 吸顶常驻、3 行瘦身与折叠 | **已验收全绿（工作区待确认 commit）** | 237 项 + `vue-tsc` + `vite build` | |
| chore | requirements 全量钉死（`59e6b64`）、基础镜像 digest 钉死、弹窗会话记忆 | `90a1947` | — |

---

## 7. 协作纪律与工程红线（完整版见 `AGENTS.md`）

1. **一卡一验、全绿入库**：每张卡跑通全量 pytest 零回归方可 commit；当前第一验收线已演进至 **237 项**（含 E27 10 项）。
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
| `docs/cards/` | 任务卡归档（B1~E27） | 每立卡/收卡时归档 |
| `.workbuddy/memory/`（不入库） | 架构侧 AI 工作日志 | 每日实质工作后追加 |

**冷启动口令**（新会话开场）：「先读取 AGENTS.md 和 HANDOVER.md，然后读 docs/cards/ 里最新一张卡，汇报理解后等我派活。」

---

## 9. 本文档信息来源

1. `docs/cards/` B1~E27 全部任务卡与验收记录；`派发归档/` 各阶段派发包全文
2. Git 提交链（`git log --oneline`，已演进至 1087a49（E26）基线 + 工作区 E27 待确认 commit）；237 项 pytest + `vue-tsc` + `vite build` 实测回执
3. `data/corpus/m0_samples/` 三本账 + `语料清单.md`、`data/corpus/sim_bids/_README.md`；`data/corpus/regulations/` 法规种子
4. `E:\agent\Contract-Review-Agent\HANDOVER_V2.md`（文档骨架参照）
