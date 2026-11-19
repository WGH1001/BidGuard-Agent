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
                 默认工业浅岩灰侧栏（支持浅岩灰/深暖炭褐灰/莫兰迪深青灰三色切换），15 个视图全部落地
④ 异步服务层    FastAPI + Pydantic + Uvicorn + SSE 实时进度流
                 bcrypt 密码哈希、HMAC-SHA256 令牌、多租户部门隔离、写操作审计留痕、
                 Redis 7 / 进程内双模任务队列、API Key 派生密钥加密落库、静态托管 frontend/dist、
                 13 组路由（含 /api/regulations 法规库、/api/similarity 交叉比对、/api/composer 编制输出）
③ 智能编排层    0 Token 确定性引擎（F1正则/F2算术沙盒/F3证照串名/bid_aligner四阶段双书对齐/E36多标书五维交叉比对/E37正向骨架与Clean定稿）
                 + 受控 LLM 分段核验（F1分段结构化/F4响应与矛盾核验/F6控标雷达/F7 ReAct追问）
                 + quote_verifier 强制原文逐字溯源锁账
                 + E2 混合检索：BM25 三级门控（词面/同义词45组/语义）+ MISSING_EVIDENCE 探针
                 + E4 法规库：法条三态核验（exact_match/fuzzy_hint/unverifiable，LLM 零裁决权）
① 数据存储层    PostgreSQL 16 主库（开发/演示无缝回退 SQLite，14 张表）、Redis 7 队列与缓存、
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
├── core/                                # 25 个功能模块（F1~F8 + 评分预估 + 写作 Skills + 指标库 + 合规脱敏 + 混合检索 + 法规库 + 交叉比对，逐项状态见 §3）
├── backend/                             # main + models(14表) + schemas + 13 组路由
├── frontend/                            # Vue3 + TS：15 个视图全部落地
├── tests/                               # 50 个测试文件（test_*.py）341 项验收线（340 passed + 1 外部路径门禁 skipped）
├── scripts/                             # build_f1_baseline.py / build_indicator_library.py / eval_f1_llm.py / probe_reasoning_efforts.py
├── data/
│   ├── corpus/m0_samples/               # 11 份语料 + baseline / ground_truth / llm_eval 三本账（§4）
│   ├── corpus/regulations/              # E4：4 部法律 JSON 种子 + originals/ 原文（含 87 号令扫描 PDF）
│   └── rules/                           # law_source/ 法规官方文本 + indicator_library/（E41/E45 常见指标与业务线口径库）
├── deploy/nginx/nginx.conf              # 静态直发 + /api/ 反代 SSE 防缓冲三件套
└── docs/                                # DEPLOY.md / cards/（任务卡归档含 B1~E45 全套定稿与验收记录）/ 待立卡-E1E2升级点定案.md
```

---

## 2. 当前基线（截至 2026-11-19）

- **开发阶段**：3 周排期全部完成 + 四十五轮可用强化（E1~E44 及 UI 收敛 `07054bb` 已入库，`E45 · 企业指标库终审工作台、F8 单章专项补写与合规脱敏扩展卡` 已完成全量实现与 341 项回归验证，留在工作区待确认 commit；“两底座 + 五业务”全部闭环）
- **Git 最新功能 Commit**：`07054bb`（UI 收敛：评分/耗时面板可折叠、左侧台账双行防截断与快捷筛选、清理界面内部卡号） / `c5811d8`（E44 评分精度精修、Rerank 向量通道与跨页任务通知）；工作区保留 **E45**（及同步文档/卡）待确认 commit；前序主线：`c31ae91`（E42）/ `093013d`（E43）/ `960c995`（E41）/ `377e37d`（E40）
- **测试基线**：**50 个测试文件共 341 项（实跑 340 passed, 1 skipped，0 失败零回归）**（在 `07054bb` 338 项基线基础上：`tests/test_e45_indicator_admin_section_compose_and_privacy.py` 新增 3 项，14 张表校验通过；唯一 skipped 为 E41 依赖旧外目录路径 `2.新agent数据法律` 的环境门禁探针）+ `npm --prefix frontend run build`（`vue-tsc --noEmit && vite build` **1881 模块**零错误构建成功）—— 第一验收线已演进至 **341 项**
- **模型网关基线**：`config/models.yaml` 当前工作区设为 `default_env: prod`（内网私有网关 `http://10.0.2.81:3000/v1`，`GLM-5.3-Flash`，`timeout: 60`）；并行提供 `demo` 环境（智谱开放平台 `glm-5.3-flash`、`embedding: embedding-3` 激活 L2 混合检索、`timeout: 60`），`LLMClient` 统一控重试与 TUN/私有 IP 报错排查提示
- **运行环境**：本机开发用项目自带 `.venv`（依赖已全量钉死）；生产走 Docker 四容器（`docker compose up -d --build`，默认宿主机 `APP_PORT=8000` + `POSTGRES_PORT=15432`，与老项目 `8080` + `5432` 并行共存不冲突）
- **快速验收命令**：

```powershell
.venv\Scripts\python -m pytest tests/ -q
# LLM 相关测试为 Mock 离线（0 Token）；真调对账需在 .env 配好 ZHIPU_API_KEY 后运行 scripts/eval_f1_llm.py
```

---

## 3. 核心模块实现清单（`core/` 下 25 个功能文件逐项状态）

| 序号 | 文件路径 | 所属模块 | 一句话职责说明 | 当前状态与实测验收数据 |
|---|---|---|---|---|
| 1 | `core/interfaces.py` | 基础契约 | `ParsedDoc`、`Clause`、`LLMResponse` 等核心数据类 | 已验收（全模块共用契约） |
| 2 | `core/parser.py` | 文档解析 | Word/PDF/TXT 解析、vMerge 去重、RapidOCR 离线缓存；E5 新增 `build_pdf_page_spans/offset_to_page`；E6 新增 `build_docx_page_spans/offset_to_docx_page`（双轨页码：印刷页 > 物理页估算 > 字符偏移诚实回退）。**E6 验收时偏移权威源已从 lxml 重建改为 python-docx 部件序列（7/7 真语料零漂移）**；**E24 新增** `parse_qa_attachment` 附件低门槛解析（取消 `OCR_MIN_CHARS=100` 报错，复用 RapidOCR 与 `.ocr.txt` 缓存）；**E27 新增** `parse_document(..., ocr_images=True, progress_cb=None)` 与 `_ocr_pdf_text(..., progress_cb=None)` 支持 Stage 1.5 全图与扫描 PDF OCR 进度回调、`.docx` 每 10 张 `.media.ocr.json` 与扫描 `.pdf` 每 5 页 `.ocr.json`（`partial=True` 断点续扫）分批原子落盘；**E29 新增** `.docx` 内嵌图片 OCR 字符区间 `ParsedDoc.media_ocr_spans` 精确挂载 | 已验收（11 份真语料解析零虚增） |
| 3 | `core/splitter.py` | 条款切分 | 条款编号与段落切分，保留精确 `start/end` 偏移 | 已验收 |
| 4 | `core/quote_verifier.py` | 溯源锁账 | `normalize_zh` 中文归一 + `verify_quote` 连续原文子串核验 | 已验收（F1/F4 防幻觉守门员） |
| 5 | `core/llm_gateway.py` | 模型网关 | OpenAI 兼容多环境调用、线程安全用量累加（`self._lock`）、`embed()` 向量接入、原生 `reasoning_effort` 透传；**E22 加固**：组合条件拒绝识别（屏蔽无关 400 误判）、单实例内存熔断记忆（不改 `self.chat_options`）、`chat_stream()` 组合去参重试、`reasoning_effort_map` 配置翻译与内部键隔离；**E24 新增**：PyMuPDF `Pixmap` 真实像素内存缩图 `encode_image_for_vlm`、`supports_vision` 透传/图像 token 占位估算与 `_is_vision_reject`/`_mark_vision_unsupported` 拒图熔断剥图重试；**E27 加固** 读取 `timeout`（默认 60.0s）+ OpenAI `max_retries=0` 与私网 IP TUN 排查提示（`_format_gateway_error_hint`）；**E40 加固** `chat()`/`chat_stream()`/`embed()` 三出口统一挂载 `privacy_sanitizer`（保全 `image_url` 多模态结构、`PRIVACY_SANITIZE_ENABLED` 默认开启、`total_usage()` 记录 `privacy_id_masked`/`privacy_bank_masked`）与 `STRICT_INNER_MODEL_ONLY` + `INNER_MODEL_EXTRA_HOSTS` 内网防外泄锁定（放行私网/`prod`/白名单，禁 `demo` 回退） | 已验收（E12 / E22 / E24 / E27 / E40 实测全绿） |
| 6 | `core/tender_extractor.py` | **F1 确定性层** | ★/▲ 分型合并、无标记模式 A/B、资格/符合/评标章定位、表格型评分细则解析（0 Token）；E18 接入统一双轨页码上下文；E23 新增通用 `open_date` 提取；**E26 新增** `extract_required_certificates` 0-Token 提取全书所需企业资质/产品认证/人员证书清单并纳入 `build_review_checklist`（附 `verify_quote` 溯源与"无需提供证明材料"免报护栏）；**E27 新增** `locate_chapters` 防无空格粘连、`classify_invalid_clause_role`（`requirement`/`eval_rule`/`submission_rule`，§2.6 扩展 `6、报价过低` 及残片识别）与 `extract_chapter4_procurement_requirements`（§2.6 新增 `_pick_procurement_chapter` 跳过前文短目录存根、`（1）/(1)/1.` 编号条支持及 Pattern B 先收缩再截断）；**E43 新增** `extract_blind_bid_requirements` 0-Token 提取暗标/盲评要求（过滤“非暗标/不采用暗标”及逗号混合否定句、区分全图禁令 vs Logo 禁令、`verify_quote` 溯源+推荐规则+挂入 `build_review_checklist['blind_bid_info']`） | **已验收**（13 份语料逐字符守恒、开标日全命中、E43 全绿） |
| 7 | `core/tender_llm.py` | **F1 LLM 层** | 候选段扩窗吸附、逐段结构化、强制溯源、段落型评分细则抽取；**E23 新增** `extract_open_date_fallback_with_llm`（局部窗开标日兜底 + `verify_quote` 与日期一致性双锁）；**E30 新增** `progress_cb` 供 F1 逐条款实时心跳与 `BaseException` 取消穿透 | **已验收**（召回率/溯源率 100%） |
| 8 | `core/price_sandbox.py` | **F2 算术沙盒** | 0 Token 报价核验（全程 Decimal 禁 float）、大写转换、限价比较；**E29 补强** `_normalize_upper` 对以 `'元'` 结尾省写 `'整'` 补 `'整'`，连 `'元'` 都缺保留触发 `UPPER_MISMATCH`；**E31 升级** 零依赖安全解析 `_parse_sandbox_amount`/`_parse_sandbox_qty`（支持逗号/货币前缀/万元/量词及科学计数法 `'3E+5'`）+ `INVALID_PRICE_FORMAT` 结构化降级 | **已验收**（差 1 分钱必报警） |
| 9 | `core/price_extractor.py` | **F2-2 提取层** | 报价表提取、vMerge 去重、万元折算、NEED_MANUAL 降级；E7~E9/E13/E14 迭代覆盖同行多金额护栏、多明细表聚合、元/年×期限、双胞胎去重、汇总行后缀识别与明细豁免；**E27 新增** `_detect_target_package_no` / `_extract_package_specific_cap`（`package_no` 多分包预算限价精准匹配）并支持复用 Stage 1.5 `parsed_doc`；**E29 新增** `'合计'` 列头识别、表前 3 段标题溯源、《开标一览表》横向合并表头跨列多行子标签取值，及 `NEED_MANUAL` 时 LLM 单元格坐标感知 + `verify_quote`/`Decimal` 重解析兜底；**E44 新增** `_extract_bid_letter_total` 精准抓取非目录区《投标函》投标总价并与《开标一览表》`bid_total` 比对，差额 >0 输出 `BID_LETTER_TOTAL_MISMATCH`（`warning` 级，排除招标限价/预算/保证金干扰句） | **已验收**（`price_cap` 命中 **11/11**；北交大 04/02 包实测 02 包精准捕获 140万 vs 139.5万差额、04 包零误报） |
| 10 | `core/qualification_scanner.py` | **F3 证照串名与暗标审计** | 证照效期、社保月份区间、跨项目串名、占位符扫描（0 Token）；E15 补双轨页码与开标日自动提取；**E26 新增** `match_required_certificates_with_ledger` 将招标所需证照与租户企业证照台账做四态保守对账；**E27 升级** `_MONTH_SINGLE_RE` 防日期回溯、新增 `resolve_effective_social_requirement` 动态推导开标日社保窗、局部社保缴费窗隔离防误吸与可选 `client` 受控语义鉴别（过 `verify_quote` 核验）；**E35 升级** `match_required_certificates_with_ledger` 适配 `is_long_term=True` 长期有效证照（恒判 `valid`、`valid_until=None`/`days_remaining=None`、防 `None vs int` 排序异常且长期证照优先排在有限日期前）；**E43 新增** `scan_blind_bid_compliance` 0-Token 暗标合规审计（检测身份线索/自定义词/手机邮箱、`.docx` 页眉页脚/底纹 `w:shd`/粗斜体/高亮/TOC 域，严格全图禁令判 `veto` 而仅禁 Logo 诚实降级 `BLIND_BID_NEED_MANUAL`，输出 `source_module='BLIND_BID'`） | **已验收**（E3R / E15 / E26 / E27 / E35 / E43 全绿） |
| 11 | `core/bid_aligner.py` | 对齐底座 | 双书条款级四阶段 + Stage 3.5 跨块回收对齐、五类要素异动探针 | 已验收（0 Token 底座；E10 防呆优化提速 50×） |
| 12 | `core/response_checker.py` | **F4 双书勾稽** | ★/▲ 逐条响应核验 + 大模型判定 + 自洽扫描；E6 接入 docx 双轨页码；E5 类名符号动态化；E19 升级“阶段 A 按序预定位 + 阶段 B 仅 `client.chat` 并发”双阶段引擎；**E27 升级** `submission_rule` 降级 `info`+`none`、同句复合质保合并与未填下划线提示、L651 `client.chat` 断连单条降级及支持复用 `tender_parsed_doc`/`bid_parsed_doc`；**E29 升级** 定位与自相矛盾跳过目录页与 `media_ocr_spans`/历史合同区、全命中对偏离表正面响应评分择优并顺延截取完整响应；**E30 新增** `progress_cb` 与取消快速撤池 `pool.shutdown(wait=False, cancel_futures=True)` | **已验收**（E19 `BIDGUARD_F4_CONCURRENCY` 默认 4；04/02 真标书定位实测通过） |
| 13 | `core/deviation_exporter.py` | **F5 偏离表** | 《技术与商务偏离表.docx》一键生成；E6 位置列升级为"第X页(印刷页Y·估算)"带 @charN 诚实回退；**E27** 传入 `include_submission_rules=False` 过滤纯递交/形式纪律，仅输出实质性技术/商务要求；§2.6 新增 keyword-only `f4_result=None`（`_resolve_f4_star_results` 兼容 `submission_rule` 长度差）支持流水线 0-LLM 预生成 | **已验收** |
| 14 | `core/rigging_radar.py` | **F6 控标雷达** | 排他性/倾向性控标参数识别（确定性正则 + 大模型④，标注"疑似"）；E18 接入统一双轨页码；**E30 新增** `progress_cb` 接入服务端取消检查 | **已验收**（E3R 修复 import；E18 打通 docx 页码；E30 接取消） |
| 15 | `core/bid_react_agent.py` | **F7 专家追问** | 多轮 ReAct 追问；E4 挂载 `cite_regulation` 第 6 工具；E18 兼容双轨页码与 `list \| dict` 传参；E19 支持三挡 `reasoning_effort`；E21 注入多轮 `history`（最近 6 轮截断+0-Token 逆序回溯）、智谱合法挡位映射与 `ask_bid_expert_stream` 流式事件引擎；E23 新增 `_coerce_action_input` 自动解包字符串化 JSON 与 `recalc_price` 缺失 `total_price` 明确拦截；**E24 扩至 7 工具**（新增 `search_attachment`）、仅当轮按选附 `image_url`、附件逐字 `verify_quote` 强制溯源与纯图视觉降级、`_parse_llm_json(strict=False)` 容错、禁吐残缺工具 JSON 与 `after_offset` 续检；**E31 补强** `recalc_price` 别名+`qty=1` `derived` 推算标注、`_safe_int_offset` 与 `ask_bid_expert_stream` 单工具异常转 `observation.error` 自愈（透传 `BaseException`）；**E32 升级** max-rounds 耗尽后追加 1 次强制收口 LLM 调用（`_FORCED_WRAPUP_PROMPT` 禁再调工具并警示过往历史合同张冠李戴 + `verify_quote` 核验 + `forced_final` 标定及纯文本/异常降级并透传 `BaseException`）；**E33 升级** 单回合最多 4 路工具 `actions` 并发保序调度、挡位上限解耦（`low=7/medium=8/high=12`）与每轮剩余回合预算/偏移桶去重空转提示；**E34 升级** 首轮快速通道（`route='direct_meta'`）+ Python 确定性防偷懒回弹护栏（`MAX_UNGROUNDED_BOUNCE_PER_TURN=1`，防空会话整理绕过与纯文本裸答）及 `grounded/fast_path` 标定 | **已验收**（E21 / E23 / E24 / E31 / E32 / E33 / E34 实测全绿） |
| 16 | `core/redline.py` | 批注回写 | Word OOXML 批注气泡 + PDF 坐标高亮；**E5 升级**：五级降级定位链（L3 标点归一/L4 逐字符 bbox 归一/L5 置信护栏≥60%且≥8字）、总览页（A4 插入 PDF 首部，全部发现编号 N01.. + LINK_GOTO 点击跳转） | **已验收**（E5 卡，111→115 项含 6 新测试） |
| 17 | `core/hybrid_retriever.py` | **E2 混合检索** | BM25 三级门控（词面精确/同义词扩展 45 组/语义兜底）+ MISSING_EVIDENCE 探针；E12 正式激活 L2 向量嵌入重排（带 `.emb.npy` 维度校验失效重算）；**E33 加固** 按 `doc_sha` 分段互斥锁 + 临时文件 `os.replace()` 原子写缓存防并发竞争 | **已验收**（E2 + E12 + E33 并发压测全绿） |
| 18 | `core/regulation_library.py` | **E4 法规库** | 8 部法律条文库：三态核验（exact_match/fuzzy_hint/unverifiable，**LLM 对真伪零裁决权**）、中文/阿拉伯条号归一、张冠李戴降级 fuzzy_hint、四格式导入（json/txt/docx/pdf，content_hash 幂等） | **已验收**（E4 卡，含 crawl 集成测试外网隔离） |
| 19 | `core/regulation_crawler.py` | **E4 法规爬取** | 静态 HTML 适配器，运行时读 `config/regulation_sources.yaml`（8 个官方 URL）；占位源诚实报"源未配置"；超时重试一次转 manual_required；SHA256 条文指纹发现官网修订 | **已验收** |
| 20 | `core/bid_similarity.py` | **E36/E38 多标书交叉比对** | 五维 0-Token 客观查重与异常一致排查（`.docx`/`.pdf` 元数据与内嵌图哈希、剔除招标底稿 12-gram 与套话后净正文 8-gram Jaccard 及 ≥25 字连续长句、非标错名/实体碰撞含身份证与手机号前 3 后 4 脱敏、Decimal 规律性报价关联与等差检测、`export_similarity_report_docx`）；**E38 升级**：解除 6 份上限仅卡 `>=2` 份，新增可选 `run_similarity_ai_review` 语义同构复核与误报排查（双侧 `verify_quote` 严审+禁洗稿/规避合规锁+报告追加第五章）；严禁洗稿/伪造指纹 | **已验收**（E36 + E38 + 7 份真语料实测全绿） |
| 21 | `core/bid_composer.py` | **E37/E38/E39/E41/E43/E45 F8 正向编制输出** | 统一蓝图 IR（`BidBlueprint`/`BlueprintSection`）+ V1/V2/AI 动态构章撰稿与排版；E38 打通 `client` 动态构章并发撰稿+三防废标锁+独立居中封面/Heading 1-3/TOC 域；**E39 精修**：单章瞬时失败最多 2 次自动重试（穿透 `PipelineCancelled`）、剔除首段重复章标题、过滤目录与投标须知/澄清/计分方法噪音并精准抽取 PDF 分值评分细则、解除章节/主题/表行与偏离表（分批 ≤12 条全覆盖）四处截断，第二部分同名证照按角色合并去重并输出「2.0 总览索引表 + 2.1~2.N 逐项独立附页（自动嵌台账 PNG/JPG 扫描图、PDF 示装订框、缺省示 A4 全幅粘贴框）」；**E41 接线**：放行显式 `knowledge_provider`（如 `IndicatorLibraryProvider`）在非 V2 `skeleton_v1` 模式下同样激活、透传指标评分提示 `scoring_ref`/`notice` 并在 `stats` 记录 `biz_line`/`use_indicator_library`；**E43 升级**：新增 `blind_bid_mode` 双模式与 6 项 `_BLIND_RULE_KEYS`+自定义屏蔽词、保守确定性清洗 `_apply_blind_bid_sanitization`（非明确业绩与外部编号不强改入人工清单）、暗标 Prompt 铁律注入及 `export_composed_bid_docx` `_strip_docx_ooxml_special_styles` 素颜渲染（清零封面底纹/页眉页脚/`w:shd`/粗斜体/`TOC` 域/`word/media/`）；**E45 升级**：新增 `compose_scope='full'\|'section'` 单章专项补写模式（跳过整书封面/资格/报价/偏离表，仅构建 `[sec_section_anchor, sec_scoring, sec_appendix]`，兼容暗标素颜与多参考附件+指标库双源注入）、`density_preset`（`concise/standard/detailed`）、`table_density`（`rich/moderate/none`）及 `tender_scoring_focus` 评分细则原文对照 | **已验收**（E37 + E38 + E39 + E41 + E43 + E45 全绿） |
| 22 | `core/composer_skills.py` | **E38/E41 写作 Skill 规范库** | 内置 5 套标书写作与动态版式 Skill（E38 通用 3 套 + E41 新增`网络安全与数据安全专精版`/`并行MaaS算力服务专精版` 含 `biz_line`）；支持 YAML Frontmatter+Markdown 与 JSON 双格式规范化解析与安全钳位（字号/行距/颜色/目标字数）、SSRF 域名白名单及 GitHub→Gitee 离线优雅降级导入 | **已验收**（E38 + E41 全套单测与 API 全绿） |
| 23 | `core/privacy_sanitizer.py` | **E40/E45 合规脱敏引擎** | 0-Token 确定性等长幂等掩码（默认 18 位 GB 11643 身份证校验 + 语境银行卡/对公账号掩码，排除项目编号/标准号/金额/无关联标签；**E45 新增** `PRIVACY_MASK_CONTACT_ENABLED` 可选 11 位大陆手机号与电子邮箱等长幂等脱敏，默认 `'0'` 保持双键零回归），原物理文件逐字节不改写；`scan_document_file` 强制 `ocr_images=False` 快速扫描计数，`sanitize_chat_messages` 保全 `image_url` 多模态结构 | **已验收**（E40 + E45 单测与实弹探针全绿） |
| 24 | `core/indicator_library.py` | **E41/E45 指标标准应答库** | 0-Token 多业务线（`common` 通用底座 + `security`/`maas`/自定义业务线专精）标准指标检索引擎，实现 `KnowledgeSnippetProvider` 协议（BM25+同义词+关键词三级门控、严格跨业务线隔离）与 `CompositeKnowledgeSnippetProvider`（指标优先+历史库互补）；内置 `_BID_MONEY_RE` 禁具体报价数字硬锁，并提供 `get_biz_line_indicator_overview` 及 **E45 在线 CRUD 与人工终审签批函数（`list_indicator_lines`/`create_indicator_line`/`upsert_indicator_entry`/`delete_indicator_entry`/`approve_indicator_line`，全字段防造假锁与内容变更自动打回 `pending_human_review`）** | **已验收**（E41 + E45 单测与 04/02 包实弹探针全绿） |
| 25 | `core/bid_scorer.py` | **E42/E44 标书评分预估与提分诊断** | 评分细则统一抽取（`collect_scoring_criteria_items` 融合 `.docx` 100 分表、`scoring_prose` 与 PDF 括号型/三列尾分型细则，**E44 补全**跨行/公式价格分抽取、大类汇总行防重计与 `_clean_factor_title` 脏前缀剥离）、三分层评估（`price_formula` 诚实置 `None`、`objective_deduction` `Decimal` 扣减 + **E44 启用 `ocr_images=True` 复用 `.media.ocr.json` 与 ISO9001/14001/45001/27001 同义词组识别扫描证照**、`subjective_quality` **E44 透传 `client=client` 激活 L2 向量精排 + 目录页/投标函清单隔离 + `.docx` 整表与正文章表聚合检索（句边界收口上限 4200 字）** + `Decimal` 钳位与 `verify_quote` 强制溯源）、F2/F3/F4/E43 `BLIND_BID_*` 一票否决先导聚合与 `.docx` 双免责声明报告导出 | **已验收**（E42/E44 单测与北交大 04/02 包真标书全图 OCR 实弹探针全绿） |

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
6. **~~F7 问答工作台布局与并发增强（E28）~~ 已交付（主体 `1ba30e0` 已入库；重命名与 F1 概览条网格补强待确认 commit）**：在 `dff8855` 入库后按 `AGENTS.md` §三.4 串行完成 `QaChatView.vue` 多会话 `cachedRuntimes` Map 并发流式生成、同会话单流令牌约束、Antigravity 风格 `Pinned Conversations`（`localStorage` 按用户名隔离）与 `Projects` 树形侧栏、`.stream-grid-spinner` 活跃指示、`backend/routers/qa.py` `_QA_DB_WRITE_LOCK` 串行落库及 `backend/models.py` `PRAGMA busy_timeout=5000`，并追加 `PATCH /api/qa/sessions/{id}` 重命名、首问自动命名与 `ExtractView.vue` F1 概览条 3×2 Grid 对齐。
7. **F2-2 / F2 阶段七/八已交付**：E7（`891b3ab`）、E8（`d56a9af`）、E9（`2137047`）、E13（`38bb1d1`）、E14（`e8d1465`）均已验收入库，11 份仿真语料相关算术盲区已清零。
8. **~~E34 · F7 快速通道判定卡~~ 已验收入库（`7da711a`）**：元问题与基于 `prior_history` 的整理类请求首轮直出 `final (route="direct_meta")`，配 Python 确定性领域关键词单次回弹护栏（`MAX_UNGROUNDED_BOUNCE_PER_TURN = 1`）及全链路 `grounded=False / fast_path=True` 微标签持久化呈现。
9. **~~E35 · 管理增强与部署诚实化卡~~ 已验收入库（`c913e40`）**：生产空库 `SEED_DEMO_DATA=0` 分层保底建 `'默认机构'`/`admin`/中性规则且零演示泄漏、`401 USER_NOT_FOUND` 鉴权补漏、部门与账号 `username` 重命名 + 禁用账号隐藏、证照 OCR 来源标注 + 去假日期 + 长期有效全链路闭环。
10. **~~E36 · 多标书防雷同对比与合规清标体检卡~~ 已验收入库（`caef678`）**：`core/bid_similarity.py` 五维 0-Token 交叉比对引擎（剔除招标公共文本+脱敏+Decimal规律报价）+ `similarity_reports` 第 12 表 + `/api/similarity` 第 12 组路由 + `SimilarityCheckView.vue` 中性UI（严锁禁洗稿/禁篡改指纹）。
11. **~~E37 · 标书编制输出与无损修订交付卡~~ 已验收入库（`caef678`）**：`core/bid_composer.py`（V1 正向骨架+台账/限价锁+V2 方案库可插拔槽位与回退）+ `core/redline.py::export_clean_revised_docx`（Run 级保样替换并清零 `w:comment*`/`comments*.xml`）+ `bid_compose_drafts` 第 13 表 + `/api/composer` 第 13 组路由 + `/api/reports/{id}/clean-bid`（`.pdf` 422 拦截）+ `BidComposerView.vue`（`/composer`）与详情/交付快捷入口。
12. **~~E38 · F8 写作 Skills 与 AI 动态编排及 E36 异步深层研判卡~~ 已验收入库（`7b7d511`）**：新增 `core/composer_skills.py`（3 套预设+GitHub→Gitee 降级+本地上传）与第 14 表 `composer_skills`、F8 AI 动态构章撰稿（防覆盖遗漏与防金钱幻觉锁+独立封面/原生 Heading/TOC+修复 Part 4 `eval_rule` 串行+紧凑待填抽屉）、E36 解除 6 份上限+AI 语义深层复核及两模块后台异步队列/取消/重启复位/`<keep-alive>` 切页不中断。
13. **~~E39 · F8 初稿质量精修与资格证明逐项附页增强卡~~ 已验收入库（`c196c84`）**：并发单章失败自动重试 2 次、剔除 AI 冗余重复章标题、过滤目录与投标须知/澄清/计分方法噪音并提取 PDF 评分细则、解除 4 处截断（偏离表分批覆盖全量条款）、第二部分证照带角色合并去重并改造为「2.0 总览索引表 + 2.1~2.N 逐项独立附页（自动嵌台账图片原件并清洗 DB/API 宿主路径）」。
14. **F8 二期 · 企业自有 `.docx` 投标母版上传与样式套打（单独立卡 Backlog）**：按 2026-11-18 架构边界裁决，一期（E38）由「Skill 版式约束 + AI 动态控制」承担排版；后续单独立卡支持企业上传自带封面/页眉页脚/预设样式表的 `.docx` 母版文件，将动态章节与表格无损套入母版占位符导出。
15. **~~E40 · 合规底座双锁卡~~ 已验收入库（`377e37d`）**：全局 `core/privacy_sanitizer.py` + `core/llm_gateway.py` 三出口（`chat`/`chat_stream`/`embed`）统一脱敏（仅限 GB 11643 18 位身份证与语境银行卡/对公账号，原文件逐字节不改写）、上传接口零 OCR `privacy_stats` 回显，以及 `STRICT_INNER_MODEL_ONLY` 内网模型防外泄双锁（拦截公网 `422 MODEL_HOST_FORBIDDEN`、放行私网/`prod`/白名单并忽略存量公网配置）。
16. **~~E41 · 双业务线专精 Skills 与常见指标标准应答库卡~~ 已验收入库（`960c995`）**：落实领导【重点】需求 3（常见指标与对应内容自动化输出）+ 需求 5（不同业务线核心指标、评分标准自动化输出与参考）。新增 `core/indicator_library.py` 三层检索与 `scripts/build_indicator_library.py` 0-Token 抽提脚本、`data/rules/indicator_library/` 三库（`review_status: pending_human_review`，实测安易 04/02 口径纠偏与槽位护栏）、扩充 `网络安全与数据安全专精版`/`并行MaaS算力服务专精版` 内置预设与 `composer_skills.biz_line` 平滑迁移、`GET /api/composer/indicators` 与非 V2 模式接线及前端评分参考预览卡片。
17. **~~E43 · 暗标信号识别与暗标防废标双模式卡~~ 已验收入库（`093013d`）**：F1 0-Token 提取暗标/盲评禁令（过滤否定句与混合否定从句，严格区分全图禁令 vs 仅禁 Logo），体检流水线以 `source_module='BLIND_BID'` 输出违规与诚实 `NEED_MANUAL` 图像核查提示；F8 增加「按通用方式 vs 按暗标生成」双模式与 6 项屏蔽规则+自定义词+OOXML 素颜渲染（零 `w:shd`/粗斜体/高亮/页眉页脚/禁止目录 `TOC`/扫描图片）。
18. **~~E42 · 标书评分预估与提分建议卡~~ 已验收入库（`c31ae91`）**：落实领导业务 2「标书评分预估（根据标书正文质量进行评估）」。新增 `core/bid_scorer.py`（统一 `.docx`/`.pdf` 评分细则并诚实输出 `score_scale_warning`，按 `price_formula`/`objective_deduction`/`subjective_quality` 三分层评估，`Decimal` 钳位与 `verify_quote` 强制溯源，聚合 F2/F3/F4/E43 `BLIND_BID_*` 一票否决预警与 Top-5 提分抓手，导出封面与页眉双免责声明 `.docx` 报告）、`BidCheckTask.score_estimate_json` 平滑加列（14 表不变）、`/api/checks/{task_id}/score-estimate*` 三端点（含重启遗留态懒收敛）及 `CheckDetailView.vue` 常驻免责声明诊断面板。
19. **~~E44 · E42 评分精度精修、Rerank 向量通道与跨页任务完成通知卡~~ 已验收入库（`c5811d8`）**：真标书（北交大 04/02 包）实测驱动的精度闭环与体验升级。① `core/bid_scorer.py` 修复五大硬伤（PDF 跨行价格分 30 分补全至 9 项=100.00、`ocr_images=True` 复用 `.media.ocr.json` + ISO 证照同义词使 04/02 包 ISO9001 正确出分 `1.00/3.00`、目录页/投标函清单隔离 + `.docx` 整表 `cell._tc` 去重与章表结构化聚合使 `Table 5`/`10.3` 合同 OCR/`Table 9`/`Table 18` 精准入窗、脏前缀清洗与标题优先分类）并透传 `client=client` 打通 L2 Rerank；② 方案 A 贯通 `user_model_configs.model_embedding`（14 表不变、复用主网关 URL/Key 与 E40 内网脱敏同锁、非阻断 `/test` `embedding_ok` 探活与 `worker` 注入）；③ `core/price_extractor.py` 增补 `BID_LETTER_TOTAL_MISMATCH` 捕获 02 包 140万 vs 139.5万差额（04 包零误报）；④ 全站新增 `stores/notifications.ts` + `App.vue` 顶栏 `<el-popover>`/`<el-badge>` 通知中心与 `ElNotification` 跨页直达提醒（贯通五大耗时任务）；并完成 `07054bb` UI 收敛（评分/耗时面板可收起、左侧台账双行防截断+快捷筛选、去除界面内部卡号）。
20. **~~E45 · 企业指标库终审工作台、F8 单章专项补写与合规脱敏扩展卡~~ 已实现验证（工作区待确认 commit）**：① `core/indicator_library.py` + `backend/routers/composer.py` + `RulesAndCertsView.vue` 第 ④ 页签打通企业指标库与自定义业务线在线 CRUD 及 `approve` 人工终审签批（所有在线新增/编辑字段强制经过 `_BID_MONEY_RE` 防造假锁自动转为 `[待人工填报：...]` 槽位，已终审库发生内容改动自动打回 `pending_human_review`，保持 14 表不变）；② `core/bid_composer.py` + `BidComposerView.vue` + `CheckDetailView.vue` 打通 WPS 风格单章节专项补写 `compose_scope='section'`（从评分面板 Top-5 提分抓手一键携带 `topic`/`guidance` 直达，跳过整书封面/资格/报价/偏离表，支持多参考附件与企业指标库双源协同、暗标 `blind_bid_mode=True` 叠加、`density_preset`/`table_density` 档位与「招标评分细则要点原文对照」折叠栏）；③ `core/privacy_sanitizer.py` + `core/llm_gateway.py` 新增 `PRIVACY_MASK_CONTACT_ENABLED` 可选手机号/邮箱等长幂等脱敏（默认 `'0'` 保持 E40 零回归）。

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
| E26 | 招标所需证照清单 0-Token 确定性提取与企业台账自动对账：`extract_required_certificates` 提取资格门槛/产品强制认证/评分资质/人员证书（带 `verify_quote` 溯源、"无需提供证明材料"免报护栏与品目清单节能产品证书捕获）并接入 `build_review_checklist`；`match_required_certificates_with_ledger` 按租户 `CompanyCertificate` 台账做四态对账（`valid`/`expiring_soon`/`expired`/`not_in_ledger`，以开标日为优先参照）；F1 前端第 ⑤ 页签与台账快捷补录联动 | **已归档（`4af8c66` / `1087a49`）** | 227 项 + `vue-tsc` + `vite build` |
| E27 | 真标书实战排障与交互体验深化：F3 社保动态窗口推导与局部语境防误拼 + 受控 LLM 语义鉴别（强过 `verify_quote`）与按 `code` 专属建议；模型网关可控 `timeout=60` / `max_retries=0` 与 TUN 诊断提示、F4 `check_bid_self_contradictions` L651 断连降级、F7 新增 `DELETE /api/qa/sessions/{id}/last-turn` 撤回本轮 + 重新生成/停止生成 + `/ask` `502 QA_GATEWAY_ERROR` 防污染；`clause_role`（`requirement`/`eval_rule`/`submission_rule`/`technical_or_commercial`）分层与第四章采购需求补齐、同句复合质保合并与未填下划线提示；分包预算限价精准匹配 + Stage 1.5 全图/扫描 PDF OCR 独立 `f_parse_ms`、`.docx` 10 图 `.media.ocr.json` 与扫描 `.pdf` 5 页 `.ocr.json`（`partial=True` 断点续扫）及冷启动 `cached_hits=0` 进度播报修复；F2–F4 台账 `.task-rail` 吸顶常驻、3 行瘦身与折叠；§2.6 验收回补 F4 后置 0-LLM 预生成 F5 偏离表与 `<keep-alive>`/`appStore.activeTaskId` 记忆/知识库双栏吸顶/真实 PDF 第四章短目录避让与长评标段先收缩后截断 | **已验收入库（`3dc8f65` / `4707f88` / `dff8855`）** | 241 项 + `vue-tsc` + `vite build` |
| E28 | F7 多会话并发与项目树形侧栏卡（多会话 `cachedRuntimes` Map 并发流式 + `Pinned Conversations`/`Projects` 树形侧栏 + `_QA_DB_WRITE_LOCK` / `PRAGMA busy_timeout=5000`；追加 `PATCH /api/qa/sessions/{id}` 重命名、`"{task.title} · 专家追问"` 首问自动命名、侧栏/顶栏行内编辑与 F1 概览条通栏+3×2 Grid 对齐） | **已验收入库（`1ba30e0` / `67b95ac`）** | 245 项 + `vue-tsc` + `vite build` |
| E29 | 感知层升级与真标书 F2/F4/OCR 实战排障卡：`ParsedDoc.media_ocr_spans` 精准区间挂载；F4 跳过目录行与内嵌历史合同图片 OCR + 全命中评分择优；`PriceSandbox._normalize_upper` 补 `'元'` 尾 `'整'` 保留缺 `'元'` 拦截；F2 增补 `'合计'` 列、表前 3 段标题、《开标一览表》横向合并表头多行子标签取值及 `NEED_MANUAL` 时 LLM 单元格坐标感知 + Python `verify_quote`/`Decimal` 重解析；Worker Stage 1.5 全缓存/零帧 OCR 进度播报与 `stats_payload` 透出；`PriceManualDialog.vue` 清除假数据改用真实预填 | **已验收入库（`063e497`）** | 250 项 + `vue-tsc` + `vite build` |
| E30 | 可中断流水线与活性心跳卡：`_PipelineCancelled(BaseException)` + `BidTaskWorker._cancel_flags` 协作式取消穿透 F1 fallback/Stage 1.5 OCR/F4 并发撤池与 `POST /api/checks/{id}/cancel`；F1 逐条款心跳进度帧（`"已判定 X / Y 条"` + `updated_at`）与前端 `>120s` 无响应判活预警；`recover_stale_tasks()` 服务启动遗留 `running` 任务自动复位；F6 控标雷达接入服务端 `_cancel_flags` 消灭僵尸 `checking` 与 `ExtractView.vue` `AbortController` 队列取消 | **已验收入库（`0756c2a`）** | 256 项 + `vue-tsc` + `vite build` |
| E31 | F7 算术沙盒容错、ReAct 工具异常自愈与体检任务重命名卡：`PriceSandbox` 零依赖解析带逗号/单位/万元/量词/科学计数法金额数量与 `INVALID_PRICE_FORMAT` 降级；`recalc_price` 别名+`qty=1` `derived` 推算标注；`ask_bid_expert_stream` 单工具 `try/except Exception` 自愈（透传 `BaseException`）与 `_safe_int_offset`；前后端 `error_type`（`gateway` vs `tool_or_internal`）精准归类；`PATCH /api/checks/{task_id}` 体检任务重命名、联动默认 `QaSession` 标题与台账/详情行内编辑 | **已验收入库（`10a63eb`）** | 260 项 + `vue-tsc` + `vite build` |
| E32 | F7 轮数耗尽强制收口、登录页安全脱敏与轻量管理员用户管理卡：`core/bid_react_agent.py` 达到 `max_rounds` 后追加 1 次强制收口 LLM 调用（`_FORCED_WRAPUP_PROMPT` 防历史合同张冠李戴 + 强制 `verify_quote` + `forced_final` 与异常优雅降级透传 `BaseException`）；`/api/health` 与 `/api/auth/health` 下发 `show_demo_login`（联动 `SHOW_DEMO_LOGIN`+`SEED_DEMO_DATA`）+ `LoginView.vue` fail-closed 隐藏演示账号；新增 6 个 `require_admin` 部门/用户管理端点（`422 INVALID_ROLE` 白名单、`409 LAST_ADMIN_PROTECTED` 先于 `CANNOT_MODIFY_SELF` 防自锁双护栏、禁用即时 `401 USER_DISABLED` 与脱敏审计）+ `UserProfileOut.tenant_name` 动态部门显示 + `UsersManageView.vue`（`/users`） | **已验收入库（`804c45b`）** | 272 项 + `vue-tsc` + `vite build` |
| E33 | F7 自主检索与并行工具调度卡：`core/bid_react_agent.py` 新增 `_extract_round_actions` 支持同轮批量 `actions`（上限 `MAX_PARALLEL_ACTIONS_PER_ROUND=4`）、`_dispatch_single_tool` + `ThreadPoolExecutor` 并发保序执行（单工具自愈 + `BaseException` 快速撤池透传）、`_EFFORT_UPSTREAM_MAP` 与 `max_rounds: Optional[int] = None` 解耦为 `low=7/medium=8/high=12`（兼容 E21 显式断言）及每轮剩余预算与偏移桶同源空转去重提示；`core/hybrid_retriever.py` 按 `doc_sha` 分段加锁+原子 `os.replace()` 写缓存；`backend/schemas.py` `QaAskRequest.max_rounds` 放宽至 `le=12`；前端 `types.ts`/`QaChatView.vue`/`ReActSteps.vue`/`QaMessageList.vue` 区分思考回合与执行步数并展示并行微标 | **已验收入库（`72cac30`）** | 280 项 + `vue-tsc` + `vite build` |
| E34 | F7 快速通道判定卡：`core/bid_react_agent.py` 扩展 `_REACT_SYSTEM_PROMPT` 放行三类无须查标书的直答并置入 Python 确定性单次回弹护栏（`MAX_UNGROUNDED_BOUNCE_PER_TURN=1`，覆盖首轮 `final` 与纯文本裸答、要求 `bool(formatted_history)` 防空会话整理绕过、`r_idx < limit` 边界与带图附件免回弹），done 帧新增 `grounded/fast_path/route`；`backend/schemas.py` `QaAskResponseOut` 与 `backend/routers/qa.py`（`/ask`、`/ask/stream` 与 `messages_json` 持久化）全链路透传；前端 `types.ts`/`QaChatView.vue`/`QaMessageList.vue` 仅对 `grounded === false` 展示「未检索标书原文 · 仅供参考」徽标 | **已验收入库（`7da711a`）** | 286 项 + `vue-tsc` + `vite build` |
| E35 | 管理增强与部署诚实化卡：`backend/database.py` 拆分 `ensure_bootstrap_admin()` 与 `seed_demo_data()`（`SEED_DEMO_DATA=0` 仅创建 `DEFAULT_TENANT_NAME` 默认 `'默认机构'` + `admin` + 中性空规则，零演示数据）；`backend/auth.py` 补齐 `get_current_user` 查库空用户即抛 `401 USER_NOT_FOUND`；新增 `PATCH /api/auth/tenants/{tenant_id}`（`rename_tenant` 审计）与 `PATCH /api/auth/users/{user_id}` 扩展 `username` 改名（`409`/`422`/`old_username`/`new_username` 审计）；`backend/models.py` 为 `CompanyCertificate` 新增 `is_long_term`、`ensure_company_certificates_e35_columns` 与 `auto_commit=False` `after_create` 事务修补；`backend/routers/rules.py`、`dashboard.py` 与 `core/qualification_scanner.py` 实现文件名兜底标注（`cert_name_source`）、长期 hint 正则及 `is_long_term` 全链路（哨兵 `9999-12-31` 对外屏蔽 `valid_until=None`）；前端 `UsersManageView.vue` / `RulesAndCertsView.vue` / `ExtractView.vue` 与部署模板/文档同步完成 | **已验收入库（`c913e40`）** | 298 项 + `vue-tsc` + `vite build` |
| E36 | 多标书交叉比对与相似度分析卡（防异常一致排查）：`core/bid_similarity.py` 五维 0-Token 引擎（docx/pdf 元数据与内嵌图哈希、剔除招标底稿 12-gram 与公文套话后 8-gram Jaccard 及连续 ≥25 字原句溯源、非标错名与身份证/手机号前 3 后 4 全链路脱敏、`Decimal` 规律性报价比例/差额与等差探测、导出《多标书交叉比对与相似度分析报告.docx》）；`similarity_reports` 第 12 表 + `/api/similarity` 第 12 组路由 + `SimilarityCheckView.vue`（`/similarity`）中性界面与全链路禁洗稿/禁篡改合规锁 | **已验收入库（`caef678`）** | **303 项** + `vue-tsc` + `vite build` |
| E37 | 标书编制输出与无损修订交付卡（正向骨架 V1 + V2 方案库槽位 & Clean 定稿导出）：新增 `core/bid_composer.py` 统一蓝图 IR（封面与投标函、企业长期/常规证照台账对齐、`Decimal` 限价下的 `[待人工填报金额...]` 报价锁、★/▲ 实质性偏离表预制、按评分细则生成方案大纲及 V2 可插拔 `KnowledgeSnippetProvider` 并支持无资料回退 `outline_stub`）与 `export_composed_bid_docx`；`core/redline.py` 提取共享 `_locate_and_split_quote_runs` 并新增 `export_clean_revised_docx`（Run 级保样替换并清零全文及 ZIP 内 `w:comment*`/`comments*.xml`，漏应答诚实归入 `manual_pending_items`）；`bid_compose_drafts` 第 13 表 + `/api/composer` 第 13 组路由 + `/api/reports/{task_id}/clean-bid`（`.pdf` 返回 `422 PDF_CLEAN_EXPORT_UNSUPPORTED`）；前端 `BidComposerView.vue`（`/composer`）与详情/交付快捷入口全链路打通 | **已验收入库（`caef678`）** | **307 项** + `vue-tsc` + `vite build` |
| E38 | F8 写作 Skills 与 AI 动态编排及 E36 异步深层研判卡：新增 `core/composer_skills.py`（3 套内置预设、YAML/MD 与 JSON 解析、SSRF 白名单及 GitHub→Gitee 离线降级导入）与第 14 表 `composer_skills`；升级 `core/bid_composer.py`（LLM 动态构章+并发撰稿、覆盖率兜底+防金钱/合同幻觉锁、剥离 Part 4 `eval_rule` 串行污染、独立居中封面/原生 Heading 1-3/TOC 域与 `append_checklist_at_end` 分离）；升级 `core/bid_similarity.py`（解除 6 份上限、新增 `run_similarity_ai_review` 双侧 `verify_quote` 与禁洗稿锁）；`backend/worker.py::submit_background_job` + `recover_stale_tasks` + `composer`/`similarity` 异步队列/中断及老库加列迁移；前端 `<keep-alive>` 纳入双视图、Skill 管理弹窗、紧凑待填抽屉、无限份数与 AI 复核开关全链路收尾 | **已验收入库（`7b7d511`）** | **312 项** + `vue-tsc` + `vite build` |
| E39 | F8 初稿质量精修与资格证明逐项附页增强卡：`core/bid_composer.py` 新增单章瞬时失败最多 2 次自动重试（`PipelineCancelled` 直通穿透）、`_strip_duplicate_leading_title` 剥离重复章标题首行、`_is_procedural_topic` / `_extract_pdf_scoring_items` 过滤目录与投标须知/澄清/计分方法噪音并精准抓取 PDF 评分细则、解除章节/主题/表行与偏离表（分批 ≤12 条覆盖全量待核对条目）四处截断；第二部分同名证照按角色合并去重（`必交资格 / 加分参考`）并改造为「2.0 总览索引表 + 2.1~2.N 逐项独立附页（自动居中嵌入台账 PNG/JPG 原件扫描图，PDF 示装订框，缺省示 A4 全幅粘贴框）」；`backend/routers/composer.py` 持久化 `blueprint_json` 前清洗宿主 `attachment_path`；`BidComposerView.vue` 第二部分增加角色级别与附页形态预览 | **已验收入库（`c196c84`）** | **317 项** + `vue-tsc` + `vite build` |
| E40 | 合规底座双锁卡：新增 `core/privacy_sanitizer.py`（18 位 GB 11643 身份证校验与语境银行账号/分组等长幂等掩码、`scan_document_file` 强制 `ocr_images=False`）；在 `core/llm_gateway.py` `chat()`/`chat_stream()`/`embed()` 三出口统一挂载脱敏（保全 `image_url` 多模态、`PRIVACY_SANITIZE_ENABLED` 开关、`total_usage()` 记录脱敏计数）与 `STRICT_INNER_MODEL_ONLY` + `INNER_MODEL_EXTRA_HOSTS` 内网锁定（放行私网/`prod`/白名单、禁 `demo` 回退、拒接公网 `UserModelConfig`）；`model_config.py` 公网拦截 `422 MODEL_HOST_FORBIDDEN`；`worker.py::resolve_effective_llm_client` 忽略存量公网配置告警回退 `prod`；`/api/checks`、`/api/extract`、`/api/qa/sessions/{id}/attachments` 与前端 `CheckTaskList.vue`/`ExtractView.vue`/`QaChatView.vue` 透传并展示只读 `privacy_stats` 且原文件字节零改动 | **已验收入库（`377e37d`）** | **321 项** + `vue-tsc` + `vite build` |
| E41 | 双业务线专精 Skills 与常见指标标准应答库卡：新增 `core/indicator_library.py`（`common/security/maas` 三层叠加与严格跨线隔离、`IndicatorLibraryProvider` 0-Token BM25/同义词三级门控、`CompositeKnowledgeSnippetProvider` 指标优先+历史库互补、`get_biz_line_indicator_overview` 评分参考透出及 `_BID_MONEY_RE` 禁具体报价硬锁）；新增 `data/rules/indicator_library/{common,security,maas}_indicators.json`（含 `scoring_ref` 与 `review_status: pending_human_review`）及 `scripts/build_indicator_library.py` 0-Token 草稿抽提脚本；`core/composer_skills.py` 扩至 5 套内置预设并支持 `biz_line`；`core/bid_composer.py` 放行显式 `knowledge_provider` 于非 V2 模式生效、透传 `notice`/`scoring_ref`；`backend/models.py` 为 `composer_skills` 新增 `biz_line` 与 `ensure_composer_skills_columns`；`backend/routers/composer.py` 幂等补种存量租户新 Skill、新增 `GET /api/composer/indicators` 及 `/generate` `use_indicator_library` 接线；前端 `types.ts`/`index.ts`/`BidComposerView.vue` 同步业务线徽标、开关与典型评分标准预览卡片 | **已验收入库（`960c995`）** | **325 项** + `vue-tsc` + `vite build` |
| E43 | 暗标信号识别与暗标防废标双模式卡：`core/tender_extractor.py` 新增 0-Token `extract_blind_bid_requirements`（过滤否定句与混合否定从句、区分全图禁令 vs 仅禁 Logo、强制 `verify_quote` 并接入 `build_review_checklist['blind_bid_info']`）；`core/qualification_scanner.py` 新增 `scan_blind_bid_compliance`（全图禁令判 `BLIND_BID_IDENTITY_LEAK` `veto`、仅禁 Logo 诚实降级 `BLIND_BID_NEED_MANUAL`，含 `.docx` 页眉页脚/底纹 `w:shd`/粗斜体/高亮/TOC 域扫描）；`backend/worker.py` 在 Stage 3 以专属 `source_module='BLIND_BID'` 入库发现并更新 `stats_payload`；`core/bid_composer.py` 为 `compose_bid_blueprint`/`export_composed_bid_docx` 新增双模式与 6 项规则+自定义屏蔽词、保守 `_apply_blind_bid_sanitization` 及 `_strip_docx_ooxml_special_styles` 素颜输出（去装饰封面/页眉页脚/底纹/粗斜体/禁止目录 `TOC`/扫描图片）；`backend/schemas.py`、`extract.py`、`composer.py`（审计日志仅记屏蔽词计数不记明文）与前端 `types.ts`/`index.ts`/`ExtractView.vue`/`BidComposerView.vue` 全链路闭环（零新增 Element Plus 组件） | **已验收入库（`093013d`）** | **330 项** + `vue-tsc` + `vite build` |
| E42 | 标书评分预估与提分建议卡：`core/bid_composer.py` 为 `_extract_pdf_scoring_items` 扩展 `include_price: bool = False`；新增 `core/bid_scorer.py`（`collect_scoring_criteria_items` 统一 `.docx` 评分表、`scoring_prose` 与 PDF 括号型/三列尾分型评分细则并输出 `score_scale_warning`；`estimate_bid_score` 三分层质量评估 + `Decimal` `[0, max_score]` 钳位 + `verify_quote` 强制溯源 + `client=None` 0-Token 降级 + `veto_alerts` 聚合 F2/F3/F4/E43 `BLIND_BID_*` 一票否决 + `top_priorities` Top-5 提分抓手；`export_score_estimate_docx` 封面与页眉双重标注「AI 质量自评预估（非最终评委实际打分）」）；`backend/models.py` 为 `BidCheckTask` 新增 `score_estimate_json` 与 `ensure_bid_check_tasks_e42_columns`（14 表不变）；`backend/schemas.py` + `backend/routers/checks.py` 新增 `POST/GET /api/checks/{task_id}/score-estimate`（含 `409 CHECK_TASK_RUNNING`/`SCORE_ESTIMATE_RUNNING` 与重启遗留态懒收敛）与 `GET /api/checks/{task_id}/score-estimate/export`；前端 `types.ts`/`index.ts`/`CheckDetailView.vue` 新增常驻免责声明的评分预估与提分诊断面板（零新增 Element Plus 组件） | **已验收入库（`c31ae91`）** | **334 项** + `vue-tsc` + `vite build` |
| E44 | E42 评分精度精修、Rerank 向量通道与跨页任务完成通知卡：`core/bid_scorer.py` 修复真标书五大硬伤（PDF 跨行/公式价格分补全使 04/02 包抽出 9 项=100.00、脏前缀清洗与标题优先分类、启用 `ocr_images=True` 复用 `.media.ocr.json` + ISO 证照同义词使 ISO9001 正确出分 `1.00/3.00`、目录页/投标函清单隔离 + `.docx` 整表 `cell._tc` 去重与章表聚合使业绩表/10.3 合同 OCR/故障表/进度表完整入窗）并透传 `client=client` 打通 L2 Rerank；`core/price_extractor.py` 增补 `_extract_bid_letter_total` 捕获 02 包投标函 140万 vs 开标一览表 139.5万 `BID_LETTER_TOTAL_MISMATCH`（04 包零误报）；方案 A 贯通 `user_model_configs.model_embedding`（14 表不变、老库平滑加列、复用主 URL/Key 与 E40 内网脱敏同锁、非阻断 `/test` 探活与 `worker` 注入、`models.yaml` `prod` 注释占位）；前端新增 `stores/notifications.ts`、`App.vue` 顶栏 `<el-popover>`/`<el-badge>` 通知中心与右上角 `ElNotification` 跨页提醒（贯通体检/评分/编制/查重/雷达五大耗时任务）及 `ModelSettingsView.vue` 向量精排配置（`components.d.ts` 仅增 `ElBadge`/`ElPopover`）；并完成 `07054bb` UI 顺手收敛 | **已验收入库（`c5811d8` / `07054bb`）** | **338 项（337 passed, 1 skipped）** + `vue-tsc` + `vite build`（1881 模块） |
| E45 | 企业指标库终审工作台、F8 单章专项补写与合规脱敏扩展卡：`core/indicator_library.py` 新增 `list_indicator_lines`/`create_indicator_line`/`upsert_indicator_entry`/`delete_indicator_entry`/`approve_indicator_line`（全字段强制经 `_BID_MONEY_RE` 防造假锁自动槽位化、原子写盘、已终审库变更自动打回 `pending_human_review`）；`core/bid_composer.py` 新增 `compose_scope='full'\|'section'`、`target_section_topic`、`target_section_guidance`、`density_preset`、`table_density` 与 `tender_scoring_focus`（单章模式仅输出 `[sec_section_anchor, sec_scoring, sec_appendix]`，兼容暗标素颜与多参考附件+企业指标库双源注入）；`core/privacy_sanitizer.py` + `core/llm_gateway.py` 新增 `PRIVACY_MASK_CONTACT_ENABLED` 可选 11 位大陆手机号与邮箱等长幂等脱敏（默认 `'0'` 零回归）；`backend/routers/composer.py` + `schemas.py` 新增 6 个 `require_admin` 指标库管理端点并扩展 `/api/composer/generate`；前端 `RulesAndCertsView.vue` 新增第 ④ 页签指标库终审台、`CheckDetailView.vue` Top-5 提分抓手一键带参跳转、`BidComposerView.vue` 新增 WPS 风格单章专项补写与篇幅/表格密度/评分对照面板 | **工作区已完成待确认 commit** | **341 项（340 passed, 1 skipped）** + `vue-tsc` + `vite build`（1881 模块） |
| chore | requirements 全量钉死（`59e6b64`）、基础镜像 digest 钉死、弹窗会话记忆 | `90a1947` | — |

---

## 7. 协作纪律与工程红线（完整版见 `AGENTS.md`）

1. **一卡一验、全绿入库**：每张卡跑通全量 pytest 零回归方可 commit；当前第一验收线已演进至 **341 项**（含 E45 3 项及 14 表检验，实跑 340 passed + 1 外部路径门禁 skipped）。
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
| `docs/cards/` | 任务卡归档（B1~E44 全套定稿与验收记录 + E45 排队草案） | 每立卡/收卡时归档 |
| `.workbuddy/memory/`（不入库） | 架构侧 AI 工作日志 | 每日实质工作后追加 |

**冷启动口令**（新会话开场）：「先读取 AGENTS.md 和 HANDOVER.md，然后读 docs/cards/ 里最新一张卡，汇报理解后等我派活。」

---

## 9. 本文档信息来源

1. `docs/cards/` B1~E43 全部任务卡与验收记录；`派发归档/` 各阶段派发包全文
2. Git 提交链（`git log --oneline`，已演进至 `093013d` E43 终版；E42 已完成全量验证留在工作区待确认 commit）；334 项 pytest + `vue-tsc` + `vite build`（1865 模块）实测回执
3. `data/corpus/m0_samples/` 三本账 + `语料清单.md`、`data/corpus/sim_bids/_README.md`；`data/corpus/regulations/` 法规种子
4. `E:\agent\Contract-Review-Agent\HANDOVER_V2.md`（文档骨架参照）
