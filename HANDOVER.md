# HANDOVER · BidGuard-Agent 交接主文档

> 本文件是 `BidGuard-Agent`（投标方标书防废标体检与偏离表生成智能体）唯一主交接文档：
> 任何 AI 或开发人员零上下文进场时，**必须先读本文档**，再按任务卡读取 `需求文档-标书防废标体检智能体PRD.md`、
> `data/corpus/m0_samples/ground_truth/_README.md` 及对应源码。

---

## 1. 项目定位与技术栈

### 1.1 一句话定位
卖方投标团队的**“封标前最后一道安全锁 + 投前控标雷达”**：上传甲方招标文件与我方投标书正本，几分钟内完成全量废标项体检（资格/符合性/★▲强制条款/报价算术/证照效期/串名穿帮/前后承诺矛盾）、在投标书原位标红批注，并一键生成填好真实页码的《技术与商务偏离表.docx》；同时支持立项前扫描招标书排他性控标参数。**严守卖方防废标视角，不碰买方采购寻源。**

### 1.2 五层技术栈（整体继承 Contract-Review-Agent 成熟生产栈）

```text
⑤ 应用展现层    Vue 3 (<script setup lang="ts">) + Vite + Element Plus (深路径按需引入) + ECharts
                 默认工业浅岩灰侧栏（支持浅岩灰/深暖炭褐灰/莫兰迪深青灰三色切换）：
                 防废标总览 / F1招标红线提取 / 封标前体检台 / F5偏离表生成 / F6投前控标雷达 /
                 F7标书专家追问 / 废标规则与证照底表 / 交付中心与审计 / 个人模型API配置 / 登录与改密
④ 异步服务层    FastAPI + Pydantic + Uvicorn + SSE 实时进度流
                 bcrypt 密码哈希、HMAC-SHA256 令牌、多租户部门隔离、写操作审计留痕、
                 Redis 7 / 进程内双模任务队列、API Key 派生密钥加密落库、静态托管 frontend/dist
③ 智能编排层    0 Token 确定性引擎（F1正则/F2算术沙盒/F3证照串名/bid_aligner四阶段双书对齐）
                 + 受控 LLM 分段核验（★大模型①②③④：F1分段结构化/F4响应与矛盾核验/F6控标雷达/F7 ReAct追问）
                 + quote_verifier 强制原文逐字溯源锁账
② 文档解析层    python-docx（Word解析/vMerge合并格去重/偏离表生成/OOXML原生批注气泡）、
                 PyMuPDF（PDF解析/页码换算/坐标高亮）、rapidocr-onnxruntime（扫描件与图片离线OCR缓存）
① 数据存储层    PostgreSQL 16 主库（开发/演示无缝回退 SQLite）、Redis 7 队列与缓存、
                 data/rules/ 招投标法规与废标规则库、data/corpus/m0_samples/ 11份政采真语料三本账
```

### 1.3 目录结构总览（当前实况 + 规划扩展）

> 标注说明：无标注 = 已实现且测试覆盖；`[规划]` = 老项目有、本项目排期内的待建件（括号内为预计节点）。

```text
BidGuard-Agent/
├── HANDOVER.md                          # 本文件：交接主文档（工程总指挥部）
├── 需求文档-标书防废标体检智能体PRD.md      # v1.3：3 周 / 15 工作日排期
├── 需求文档-标书防废标体检.docx            # word 同步版
├── 流程图-投标标书.html / 投标原型图.html   # 业务流程图与前端原型（1:1 落地基准）
├── README.md                            # [规划扩充] 项目门面（当前 3 行占位）
├── INTERFACE.md                         # [规划] backend 建表立卡时建立，锁死 core↔backend 契约
├── FRONTEND.md                          # [规划] frontend 脚手架立卡时建立
├── docker-compose.yml                   # [规划 第3周 11.20-11.24] 四容器编排（nginx:alpine + app + postgres:16 + redis:7）
├── Dockerfile                           # [规划 第3周] Python 3.12 + rapidocr 离线模型
├── .env.example                         # [规划扩充] 5 → 14 变量（完整清单见 §8.2）
├── requirements.txt                     # 16 包定稿
├── config/models.yaml                   # 模型网关 demo=智谱 / prod=内网私有化 配置
├── core/                                # 17 个模块：F1~F4 已实现，F5~F7 空占位（逐项状态见 §3）
├── backend/
│   ├── main.py / auth.py / utils/crypto.py   # 骨架桩（auth 仅密钥守卫，无路由无 ORM）
│   ├── models.py                        # [规划 第2周] 8 张表 ORM（§6.2）
│   ├── schemas.py                       # [规划 第2周] Pydantic 请求/响应契约
│   ├── database.py                      # [规划 第2周] SQLAlchemy 双驱动（PG16 主 / SQLite 回退）
│   └── routers/                         # [规划 第2周] 10 组路由（§6.2）
├── frontend/                            # [规划 第2-3周] Vue3 + Vite + Element Plus 脚手架
│   └── src/views/                       # [规划] 8 大视图（§6.3，对照 投标原型图.html 1:1 落地）
├── tests/                               # 7 个测试文件 60 项全绿
├── scripts/                             # build_f1_baseline.py（F1 候选构建）/ eval_f1_llm.py（对账）
├── data/
│   ├── corpus/m0_samples/               # 11 份语料 + baseline / ground_truth / llm_eval 三本账（§4）
│   └── rules/law_source/                # 4 部法规官方文本
├── deploy/
│   └── nginx/nginx.conf                 # [规划 第3周] 静态直发 + /api/ 反代 SSE 防缓冲三件套
└── docs/                                # [规划 封版周] DEPLOY.md / OPERATIONS.md 归档
```

---

## 2. 当前基线（截至 2026-11-04）

- **当前开发阶段**：3 周（15 个工作日，2026-11-04 ~ 2026-11-24）排期的第 1 天，已完成原计划前 8 个工作日的内核任务（M0 语料构建 + F1 + F2 + F2-2 + F3 + F4 全部落地验收）
- **Git 最新 Commit**：`98d4928`（演进链：`ab66c66` F1 确定性基线 → `737a645` F1 LLM 层 → `48d8fdf` F1 分歧收口与评分表 → `43ce47e` F2 算术沙盒 → `1e3930e` F2-2 报价提取 → `7a623ae` F3 证照串名 → `98d4928` F4 双书勾稽与矛盾核验）
- **分支与远端**：主分支 `main`，已推送远端仓库
- **测试基线**：**7 个测试文件、60 项 pytest 单元测试全绿（100% 通过，零回归）**
- **模型网关基线**：`config/models.yaml` 默认 `default_env: demo`（智谱 `glm-5.3-flash` 直连，`reasoning_effort: low`）；预留 `prod` 内网私有网关（`http://10.0.2.81:3000/v1`，`GLM-5.3-Flash`）
- **快速验收命令**：

```powershell
# 首次运行需先安装依赖（本项目 .venv 尚为空壳；当前开发机借用 Contract-Review-Agent/.venv 执行）
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m pytest tests/ -q
# LLM 相关测试为 Mock 离线（0 Token）；真调对账需在 .env 配好 ZHIPU_API_KEY 后运行 scripts/eval_f1_llm.py
```

---

## 3. 核心模块实现清单（`core/` 下 17 个文件逐项状态）

| 序号 | 文件路径 | 所属模块 | 一句话职责说明 | 当前状态与实测验收数据 |
|---|---|---|---|---|
| 1 | `core/__init__.py` | 包初始化 | `core` 算法引擎包导出标识 | 已就绪 |
| 2 | `core/interfaces.py` | 基础契约 | 定义 `ParsedDoc`、`Clause`、`LLMResponse` 等核心数据类（摘自既有项目） | 已验收（全模块共用契约） |
| 3 | `core/parser.py` | 文档解析 | Word/PDF/TXT/图片解析与 RapidOCR 扫描件离线缓存；含针对政采 docx `vMerge` 合并单元格按 `cell._tc` 本体去重修复 | 已验收（11 份真语料解析零虚增） |
| 4 | `core/splitter.py` | 条款切分 | 合同/标书正文按条款编号与段落切分并保留精确 `start/end` 偏移（摘自既有项目） | 已验收（供 `bid_aligner` 调用） |
| 5 | `core/quote_verifier.py` | 溯源锁账 | `normalize_zh` 中文归一化与 `verify_quote` 连续原文子串核验（零外部依赖） | 已验收（F1/F4 防幻觉核心守门员） |
| 6 | `core/llm_gateway.py` | 模型网关 | OpenAI 兼容协议多环境调用、重试与 Token/人民币成本累计（读取 `config/models.yaml`） | 已验收（F1 实测 178 次调用仅 0.231 元） |
| 7 | `core/redline.py` | 批注回写 | Word OOXML 原生批注气泡与 PDF 坐标高亮回写引擎（摘自既有项目） | 已就绪（待与 F5/封版交付路由联调） |
| 8 | `core/bid_aligner.py` | 对齐底座 | 双书条款级四阶段 + Stage 3.5 跨块回收对齐流水线与五类要素异动探针（1014 行，已剥离旧项目 `rule_engine` 耦合） | 已验收（0 Token，F4/F5 双书勾稽底座） |
| 9 | `core/tender_extractor.py` | **F1 确定性层** | ★/▲ 标记条款分型合并、无标记模式 A/B 提取、资格/符合/评标章定位、docx 表格型评分细则解析（0 Token） | **已验收**（11 份语料逐字符守恒、零重复；表格型评分表 4 份解析成功，3 份精确=100 分） |
| 10 | `core/tender_llm.py` | **F1 LLM 层** | 候选段扩窗吸附、逐段结构化（含 `consequence` 四分类与断行提示）、强制 `quote_verifier` 溯源、段落型评分细则抽取 | **已验收**（11 份语料召回率 100%、溯源率 100%、对账分歧由 9 收敛至 1） |
| 11 | `core/price_sandbox.py` | **F2 算术沙盒** | 与表格解析完全解耦的 0 Token 报价核验（全程 `Decimal` 禁 `float`）：分项单价×数量、累加=表合计=一览表总价、央行规范大写转换（兼容正/整/圆）、最高限价 | **已验收**（13 项单测全绿，差 1 分钱/大写错 1 字 100% 报警） |
| 12 | `core/price_extractor.py` | **F2-2 提取层** | 从招投标文档提取报价表结构：表头映射、`vMerge` 去重、合计行隔离、万元→元折算、`require_unit` 防幻影金额、空白模板 `NEED_MANUAL` 降级 | **已验收**（8 项单测全绿；11 份真语料 `price_cap` 命中 9/11，空白模板 100% 正确降级） |
| 13 | `core/qualification_scanner.py` | **F3 证照串名** | 0 Token 四大防线：证照有效期（6 种日期格式，过期 error / ≤30天 warning）、社保月份区间展开核验、跨项目串名扫描（含 `target_buyer` 防自伤）、占位符扫描 | **已验收**（16 项单测全绿，过期天数与缺失月份精确输出） |
| 14 | `core/response_checker.py` | **F4 双书勾稽** | ★/▲ 条款逐条响应核验（`bid_aligner` + 条款号/标题锚点定位 + 大模型①判定 `RESPONSE_OK/MISSING_RESPONSE/CONTRADICTION`）+ 投标书内部质保期（含反序变体）/总价/响应时间自洽扫描（0 Token + 大模型②） | **已验收**（commit `98d4928`；真机端到端实测 2 正响应/1 漏答/1 矛盾全部精准；11 份语料 178 条要求自对齐 100% 定位） |
| 15 | `core/deviation_exporter.py` | **F5 偏离表** | 《技术与商务偏离表.docx》双书对齐与标准 Word 表格一键生成器 | **空占位**（下一张卡立即开发） |
| 16 | `core/rigging_radar.py` | **F6 控标雷达** | 投前立项辅助流：识别招标书排他性/倾向性控标参数（★大模型④，标注“疑似”） | **空占位**（待开发） |
| 17 | `core/bid_react_agent.py` | **F7 专家追问** | 标书专家多轮 ReAct 追问（★大模型③，跨双书检索评分项与响应内容并返回精确页码坐标） | **空占位**（待开发） |

---

## 4. 数据资产与“三本账”读写纪律

### 4.1 11 份政采真实招标书语料（`data/corpus/m0_samples/`）
全库共 11 份真实政府采购招标文件（8 份 `.docx` + 3 份 `.pdf`：亳州学院、江苏科技一件事、江苏固废、南京医科大四院病理、无锡梁溪档案、无锡新吴自然资源、徐州智慧文旅、连云港数智公积金、芜湖无为档案、滁州物业维修资金、滁州城职院数智托育），公告发布日期与官方 URL 均已在 `data/corpus/m0_samples/语料清单.md` 100% 闭环登记。

### 4.2 三本账目录职责与严禁混写铁律

| 账本目录 | 性质与产生方式 | 读写权限铁律 |
|---|---|---|
| `data/corpus/m0_samples/baseline/` | 机器确定性候选清单（`scripts/build_f1_baseline.py` 产物 + `_summary.md`） | **可随时由脚本重跑覆盖** |
| `data/corpus/m0_samples/ground_truth/` | 人工三轮交叉复核定稿的 F1 验收真值基准（含 `_README.md` 改判台账） | **只读圣域：任何自动化脚本严禁写入或覆盖** |
| `data/corpus/m0_samples/llm_eval/` | F1 LLM 分段结构化产物（`structured_*.json` + `_eval_report.md`） | 已入库 Git，作为下游 F4/F5 的**离线零成本测试桩（Fixture）**与回归对账基线 |

**提取器升级标准流程**：修改 `tender_extractor.py` 正则 → 重跑 `baseline/` → 与 `ground_truth/` 做人工 diff 审核 → 人工确认无误后手动同步更新 `ground_truth/` 并在 `_README.md` 登记改判原因。

### 4.3 `kind` 三分类消费契约（所有下游模块必读）
- `requirement`：投标人须逐条响应的实质要求（含滁州托育 16 项 ▲ 加分项，评分后果由 `structured.consequence` 的 `veto / deduction / bonus / none` 区分） → **F1/F4/F5 正常消费**；
- `usage_note`：标记使用说明语（评分规则说明、偏离表填表说明、节能品目清单说明等） → **F4/F5 必须排除**；
- `table_marker`：附表行标记碎片（中小企业划型表行尾 `★`、芜湖标的表 `▲中共无` 断行碎片） → **F4/F5 必须排除**。

### 4.4 法规底座（`data/rules/law_source/`）
已归档 4 部招投标核心法律法规官方文件（财政部第 87 号令《政府采购货物和服务招标投标管理办法》、《政府采购法》（2014 修正）、《政府采购法实施条例》（2015）、《招标投标法》（2017 修正））。

---

## 5. 已知局限与遗留待办（Backlog）

1. **F1 评分细则表段落型抽取精修**：
   - 表格型评分表（4 份 docx）已 0 Token 完美封口（3 份精确=100 分，滁州托育 90 分缺的 10 分价格分公式在正文段落中）；
   - 段落型评分细则（7 份无标准表格语料）走 LLM 锚点窗口抽取时存在 **输出粒度方差**（如梁溪首跑 27 项 100 分、二跑 19 项 86 分）及局部跨窗超计（亳州 115 分、滁州物业 240 分）。
   - **待办动作**：后续立独立精修卡（加入“分值合计 ≠ 100 分自动告警重抽”、固定 `temperature=0`、表格型与正文价格分自动拼合）。
2. **F2-2 剩余 2 份语料的 `price_cap` 特殊措辞适配**：
   - 11 份语料中 `price_cap` 自动提取已命中 9/11，仅 `江苏科技一件事` 与 `江苏固废` 两份因表格跨格或特殊措辞未命中，后续抽样补齐正则分支。
3. **两处语法不可分的人工改判保护**：
   - **芜湖 `@26309: ▲中共无`**（标的表采购人名称 PDF 断行碎片，与滁州物业真功能项 `▲残值回`/`▲资金使` 语法同构） 与 **南京病理 `@33621: ★（二）商务要求`**（纯章节标题行，与同书大量 `★（1）常规制片` 短真要求语法同构）：提取器保持保守不硬判，依靠 `ground_truth/` 人工改判锁死。

---

## 6. 待开发全栈规划（对标并超越上一项目功能体量）

为确保 3 周（11.04 ~ 11.24）交付成品在功能丰富度、交互完整度与工程规范上全面看齐并超越 `Contract-Review-Agent`，剩余工作按以下四层推进：

### 6.1 核心引擎层收尾（`core/` · 第 2 周）
1. **F5 `core/deviation_exporter.py`（下一张卡）**：
   - 输入 F4 核验结果与招标文件要求清单（含 ★/▲ 参数、模式 A/B 条款、商务要求）；
   - 自动映射生成标准五列/六列 Word 表格（`序号 | 招标文件条款号 | 招标文件要求 | 我方投标文件响应内容 | 偏离情况（无偏离/正偏离/负偏离/漏应答告警） | 投标书响应页码/位置索引`）；
   - 使用 `python-docx` 输出排版规范的《技术与商务偏离表.docx》，并联动 `core/redline.py` 在投标书原件写入 Word 原位标红批注气泡。
2. **F6 `core/rigging_radar.py`（投前控标雷达 · ★大模型④）**：
   - 结合确定性排他特征词正则与大模型④语义研判，识别招标书排他性/倾向性控标参数；
   - 输出 `值得投 / 谨慎投（标注“疑似”，不替老板拍板）` 评估结论及可疑参数清单。
3. **F7 `core/bid_react_agent.py`（标书专家 ReAct 追问 · ★大模型③）**：
   - 挂载招投标检索工具（评分细则检索、投标书响应与证照定位、报价与效期复算、政采法规检索），回答强制附带双书页码/偏移坐标。

### 6.2 后端服务与 ORM 数据层（`backend/` · 8 张表 + 10 组路由）
- **数据库 8 张核心表（`backend/models.py`）**：
  1. `tenants`（多租户投标部门：政企系统集成投标部 / 弱电智能化工程部 / 智能装备销售中心）
  2. `users`（用户与角色鉴权：bcrypt 密码哈希）
  3. `bid_check_tasks`（封标体检与投前控标任务主表：支持双文件关联、SSE 进度流状态机 `created -> extracting -> checking -> grading -> delivered`、报价表 `NEED_MANUAL` 人工回填）
  4. `bid_findings`（检出废标项/扣分项/控标疑似项明细表：含双书摘录、偏移、页码、采纳/驳回反馈）
  5. `bid_rule_configs`（项目核查参数与三模自定义废标红线表：最高限价、开标日、招标人全称、社保要求月份 + 自定义红线）
  6. `company_certificates`（企业投标常用证照与业绩效期台账：支持到期预警与一键补入偏离表）
  7. `qa_sessions`（F7 标书专家多轮追问历史会话持久化表）
  8. `audit_logs` & `user_model_configs`（安全审计留痕表 + 用户级自定义大模型 API Key 加密配置表）
- **后端 10 组 API 路由（`backend/routers/`）**：
  `/api/auth`、`/api/dashboard`、`/api/checks`、`/api/extract`、`/api/deviation`、`/api/radar`、`/api/qa`、`/api/rules`、`/api/reports`、`/api/model-config`。

### 6.3 前端工作台计划（`frontend/` · 对照根目录 `投标原型图.html` 1:1 落地）
- **视觉与交互基线**：Vue 3 + TypeScript + Element Plus（深路径按需引入）；默认采用**工业浅岩灰侧栏 (`#e2e8f0`) + 高对比度深色底栏文字**，顶栏支持一键切换「工业浅岩灰 / 深暖炭褐灰 / 莫兰迪深青灰」三种侧栏色调。
- **8 大核心业务视图（`frontend/src/views/`）**：
  1. `DashboardView.vue`：防废标体检总览（4 统计卡 + 封标前六大一票否决防线健康度矩阵 + 近期截标倒计时与控标预警看板 + 在途项目台账）；
  2. `ExtractView.vue`：F1 招标文件废标红线与 ★/▲ 强制条款四张结构化清单核对页；
  3. `CheckDetailView.vue`：封标前防废标体检主工作台（6 步流水线进度条 + 左栏投标书原位标红预览 + 右栏废标风险卡片 + 报价表 `NEED_MANUAL` 人工回填弹窗）；
  4. `DeviationView.vue`：F5 《技术与商务偏离表》双书对齐预览与一键导出 `.docx` 工作台；
  5. `RadarView.vue`：F6 投前控标雷达立项评估页；
  6. `QaChatView.vue`：F7 标书专家多轮 ReAct 追问中心（KeepAlive 切页防丢）；
  7. `RulesAndCertsView.vue`：废标规则配置与企业投标证照底表页；
  8. `ReportsAndAuditView.vue`：封标交付中心（原位标红投标书 + 偏离表 + 人工终核签字单 ZIP 打包）与安全审计页。

### 6.4 容器化部署层计划（排期第 3 周：2026-11-20 ~ 11-24）
- 继承上一项目验证成熟的 **Docker 四容器 Nginx 统一入口架构（T62）**：
  - `Dockerfile`：Python 3.12 镜像（内置 `rapidocr-onnxruntime` 离线模型）；
  - `docker-compose.yml`：`nginx:alpine`（对外监听 `APP_PORT:-80` 反代 `app:8000`，`app` 端口不对外暴露）+ `app` + `postgres:16` + `redis:7`；
  - `deploy/nginx/nginx.conf`：配置静态资源直发与 `/api/` 反代 SSE 防缓冲三件套（`proxy_buffering off` / `proxy_cache off` / `300s` 超时）。

---

## 7. 协作纪律与工程红线（从本项目已有代码注释与实操提炼）

1. **一卡一验、全绿入库**：每张任务卡完成后必须跑通全量 `pytest` 零回归方可 commit；当前第一验收线为 **60 项 pytest 全绿**。
2. **凡能算的绝不进大模型（0 Token 优先铁律）**：分项乘加、总价累加、人民币大小写转换、最高限价比较、证照日期差、社保月份集合覆盖、跨项目串名匹配 100% 由确定性 Python 代码完成。
3. **金额计算全程 `Decimal` 禁 `float`**：所有金额运算统一使用 `decimal.Decimal` 并按 `ROUND_HALF_UP` 量化至 `0.01` 元（差 1 分钱必须报警）；自由文本提取金额必须启用 `require_unit` 防止把条款序号误抓为金额。
4. **LLM 只吃局部窗口 + 强制 `quote_verifier` 溯源**：严禁将 200 页整本标书直塞大模型（防 R-01 注意力衰减）；所有 LLM 摘录必须经过 `verify_quote()` 原文连续子串核验，未通过核验的一律清空并标 `quote_verified=False`。
5. **宁缺毋滥的显式降级原则**：报价表为空白模板或异形合并格无法对齐时必须返回 `status='NEED_MANUAL'`；`client=None` 时所有模块必须支持 0 Token 降级运行；页码仅对 PDF 用 `pymupdf` 换算真实页码，docx 页码一律置 `None`，严禁编造页码。
6. **数据账本隔离与表格去重铁律**：任何自动化脚本严禁覆写 `ground_truth/`；docx 表格遍历必须按 `cell._tc` 元素本体引用入集合去重（严禁用 `id()` 临时生命周期防止对象回收后 id 复用导致去重不稳定）。
7. **跨平台与编码卫生**：测试读写文件统一指定 `encoding='utf-8'` 与 `newline='\n'`；库模块静默工作（只抛异常不 `print`）；PowerShell 5.1 不支持 `&&`，多条命令拆行执行。

---

## 8. 文档补齐计划与 `.env.example` 扩充清单

### 8.1 文档体系规划
- **根目录现存文档**：
  - `HANDOVER.md`（本文件：当前基线、模块清单、三本账规则、待开发全栈规划与工程红线）
  - `需求文档-标书防废标体检智能体PRD.md`（v1.3 定稿，3 周 / 15 工作日排期）
  - `需求文档-标书防废标体检.docx`（Word 同步版）
  - `流程图-投标标书.html` & `投标原型图.html`（三色流程图、竞品分析、浅岩灰侧栏前端工作台原型）
- **后续按节点立卡补齐的文档**：
  1. `INTERFACE.md`：在启动 `backend/` ORM 建表与路由层立卡时建立，锁死 `core/` 17 个模块与 `backend/schemas.py` 的契约；
  2. `FRONTEND.md`：在启动 `frontend/` Vue 3 脚手架立卡时建立；
  3. `docs/DEPLOY.md` & `docs/OPERATIONS.md`：在第 3 周（11.20-11.24）容器化封版时放进 `docs/` 目录。

### 8.2 `.env.example` 待扩充变量清单（搭建 `backend/` 路由与容器层时补齐）
当前 `.env.example` 含 5 个基础变量（`ZHIPU_API_KEY`、`PRIVATE_API_KEY`、`AUTH_SECRET`、`DATABASE_URL`、`SEED_DEMO_DATA`），进入 `backend/` 阶段时扩充为以下完整模板（严禁填入真实密钥）：

```ini
# ── 1. 大模型网关配置（对应 config/models.yaml）──
MODEL_ENV=demo
ZHIPU_API_KEY=your_api_key_here
PRIVATE_API_KEY=

# ── 2. 安全鉴权与多租户配置 ──
AUTH_SECRET=change_me_in_production_at_least_32_chars
SSO_SECRET=change_me_for_platform_sso
ADMIN_INITIAL_PASSWORD=ChangeMe123!

# ── 3. 数据库与 Redis 队列缓存配置 ──
DATABASE_URL=sqlite:///./data/bidguard.db
REDIS_URL=redis://redis:6379/0

# ── 4. 服务端口、CORS 与运行态开关 ──
APP_PORT=80
BACKEND_INTERNAL_PORT=8000
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:80
LOG_LEVEL=INFO
SEED_DEMO_DATA=1
SHOW_DEMO_LOGIN=1
```

---

## 9. 本文档信息来源清单

1. `E:\agent\BidGuard-Agent\需求文档-标书防废标体检智能体PRD.md`（v1.3 定稿：项目定位、技术选型、F1~F7 定义、3 周 / 15 工作日排期）
2. `E:\agent\BidGuard-Agent\流程图-投标标书.html` & `投标原型图.html`（三色业务流程图、浅岩灰侧栏前端原型）
3. `E:\agent\BidGuard-Agent\data\corpus\m0_samples\ground_truth\_README.md`、`baseline\_summary.md`、`llm_eval\_eval_report.md`（11 份政采语料三本账、逐字符守恒对账、人工改判台账与 LLM 评测报告）
4. `E:\agent\BidGuard-Agent\core\` 下源码文件：`parser.py`、`interfaces.py`、`bid_aligner.py`、`tender_extractor.py`、`tender_llm.py`、`price_sandbox.py`、`price_extractor.py`、`qualification_scanner.py`、`response_checker.py` 及 `tests/` 单测回执（commit `98d4928`，60 项 pytest 全绿）
5. `E:\agent\BidGuard-Agent\config\models.yaml` & `.env.example`（模型网关 demo/prod 配置与环境变量模板）
6. `E:\agent\Contract-Review-Agent\HANDOVER_V2.md`（参考文档骨架、五层架构、四容器 Nginx 部署规范与工程红线体系）