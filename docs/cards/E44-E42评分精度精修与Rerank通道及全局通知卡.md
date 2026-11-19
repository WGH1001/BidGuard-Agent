# E44 · E42 评分预估精度精修、Rerank 向量通道打通与跨页任务完成通知卡

> 状态：已完成全量实现、04/02 包真标书实弹探针与全量回归验收，留在工作区待确认 commit（已落地**方案 A**：复用主网关 URL/Key 增加 `model_embedding`） ｜ 代码基线：**E42 `c31ae91`（334 项 pytest 全绿 + `vite build` 1865 模块零错误）** ｜ 串行约束：**E44 先行（与 E45 共享 `types.ts`/`schemas.py`/`CheckDetailView.vue`/`BidComposerView.vue`），E45 须在 E44 验收入库后启动**
> 立卡动因：以真实政采语料（`E:\agent\2.标书agent数据库，资料\安易—04包-工业物性数据应用开发平台.docx`、`安易—02包.docx` 及对应两份《标书预估得分与提分诊断报告.docx》）实测对账，暴露出 E42 的 **5 个代码级精度硬伤**、**Rerank 链路未透传 `client` + 无向量配置入口**、**《投标函》总价与《开标一览表》不一致未拦截** 以及 **全站耗时任务缺少跨页顶部完成通知**，需一次性精修闭环。

---

## 1. 核心目标

1. **修复真标书实测暴露的 E42 五大精度硬伤（实事求是，消灭冤判与错漏）**：
   - **硬伤 1（PDF 价格分跨行漏抓致总分 70≠100 误报）**：补全「`序号 + 价格/报价 + 跨行（XX 分）/独立行 XX 分`」提取，并准确识别 `技术（55 分）`/`商务要求（13 分）`/`政策分（2 分）` 大类汇总头；04/02 包必须完整提取 **8 项 = 100.00 分**（`price 30 + tech/service/qual/biz 70`），`score_scale_warning` 为 `None`；
   - **硬伤 2（`ocr_images=False` 屏蔽图片 OCR 致已附 ISO9001 与 15 张业绩合同扫描件失明）**：`estimate_bid_score` 解析投标文件时默认允许消费图片 OCR 文本与 `.media.ocr.json` 缓存，且在 `objective_deduction` 中识别扫描件证照（例如 04/02 包 `10.2 资格证书` 内嵌 ISO9001 图片，正确判 `1.00 / 3.00` 分而非 `0.00`）；
   - **硬伤 3 & 4（BM25 行级切块误中目录页/投标函、800 字断句幻觉）**：引入目录页/目录行隔离 + **章节与整表结构化聚合检索**，按评分因素锚定正文章节标题（如 `6 业绩案例一览表` / `10.3业绩证明` / `实施方案` / `售后服务方案` / `项目进度计划` / `培训方案`）、紧邻整张 Markdown 展平表格（`Table 5`/`Table 9`/`Table 10`/`Table 16`/`Table 18` 等）、紧随图片 OCR 块及章节正文（窗口扩展并按句号/换行边界收口，严禁拦腰斩断词语导致误判原文截断）；
   - **硬伤 5（PDF 评分因素名粘连上一项规则尾碎片）**：清洗前导残片（如 `容清单、签署页，复印件加盖公章）企业管理体系` → `企业管理体系`、`商务要求（13 分）相关业绩` → `相关业绩`、`政策分（2 分）节能环保` → `节能环保`）。
2. **打通 L2 向量精排（Rerank）全链路通道（有向量模型秒激活，无向量模型零报错自动降级）**：
   - 修复 `core/bid_scorer.py::_retrieve_verified_local_quotes` 硬编码 `client=None` 缺陷，将 `client` 透传给 `hybrid_retrieve_windows(..., client=client)`；
   - 打通模型配置 `model_embedding`（`backend/models.py`、`backend/schemas.py`、`backend/routers/model_config.py`、`backend/worker.py`、`frontend/src/views/ModelSettingsView.vue`、`config/models.yaml` 注释占位），让公司内网网关一旦上架向量模型即可零改代码激活 E12 L2 混合检索精排（未配置时严格沿用 E12 快速失败静默回退 Level 1 BM25+同义词）。
3. **补强 F2 跨处总价一致性审计（堵住 02 包「投标函 140万 vs 开标一览表 139.5万」真实商务雷区）**：
   - 在 `core/price_extractor.py` 增补确定性《投标函》总价提取与《开标一览表》`bid_total` 比对，发现差额（如 02 包差 5,000 元）且排除招标预算/限价条款后输出 `BID_LETTER_TOTAL_MISMATCH`（`warning` 级提示）。
4. **全站后台耗时任务「顶部全局完成通知 + 顶栏通知中心」**：
   - 无论用户停留在任何页面，当 `F2-F4 体检`、`E42 评分预估`、`F8 编制生成`、`E36 多标书查重`、`F6 控标雷达` 进入完成/待人工/失败终态时，顶部右上角统一弹出 `ElNotification` 提醒卡片（带核心结论与点击直达）并同步写入 `App.vue` 顶栏通知徽标/下拉清单，切页不丢。

---

## 2. 技术设计明细

### §2.1 `core/bid_scorer.py`（精度精修 + Rerank `client` 透传 + OCR 与章表级证据聚合）

1. **PDF 评分细则提取与脏前缀清洗（解决硬伤 1、5）**：
   - 扩展 `_extract_prose_line_criteria` 与 `collect_scoring_criteria_items`：
     - 支持 `序号（可选） + 价格/报价 + 跨行（XX 分）` 与 `价格（XX 分）` 独立表行抽取（解决 04/02 包第 27 页 `1\n价格\n（30 分）` 漏抓）；
     - 识别并跳过紧邻子项的大类汇总行（如 `技术（55 分）`、`商务要求（13 分）`、`政策分（2 分）`，防止重复累计为 170 分）；
     - 新增 `_clean_factor_title(raw_factor, detail)`：剥离因 PDF 双列/多行折行粘连的前导规则残片（如 `……）\s*`、括号后半片 `容清单、签署页，复印件加盖公章）`、前导大类标签 `商务要求（13 分）` / `政策分（2 分）`），保证 `factor` 简洁准确（同时将大类信息留在 `section` 字段不丢失）；
     - 若总分与分项和一致，严格返回 `score_scale_warning=None`。
2. **复用图片 OCR 文本与 `.media.ocr.json` 缓存（解决硬伤 2）**：
   - `estimate_bid_score` 在自行解析 `bid_path`（`bid_parsed_doc is None`）时改用 `parse_document(str(b_path), ocr_images=True)`：
     - 当主体检任务 Stage 1.5 已跑完，磁盘已存在 `<bid>.media.ocr.json`，直接 100% 命中缓存（零新增 OCR 耗时）；
     - 若离线直接调用（无缓存），按现有 `parser.py` 增量机制识别内嵌证照与合同扫描件并原子落盘复用；
   - 在 `objective_deduction` 证书/体系核验中，明确同时检索正文表格与图片 OCR 段落；识别 `质量管理体系` 时同步兼容 `ISO9001` / `ISO/IEC 9001` / `GB/T19001`（同理环境 `ISO14001`/`GB/T24001`、职业健康安全 `ISO45001`/`GB/T45001`、`信息安全管理体系`/`ISO27001`）；对存在扫描图但缺某几项证书的场景逐项扣除未提供分值（如 3 项缺 2 项 → `1.00 / 3.00` 分，诚实列明已检出证书名与缺失项）。
3. **目录隔离 + 章表级证据聚合 + Rerank 透传（解决硬伤 3、4 与 Rerank 打通）**：
   - 修改 `_retrieve_verified_local_quotes(query, bid_text, bid_page_ctx, *, client=None, top_k=3, factor='', category='') -> tuple[list[str], list[dict[str, Any]]]`：
     - **透传 `client=client`** 至 `hybrid_retrieve_windows(query, bid_text, client=client, top_k=max(top_k, 5), min_score=0.28, window_pad=320)`；
     - **过滤目录行**：新增 `_is_toc_line(line)` / `_is_toc_window(bid_text, start)`（检测含 `\t页码`、`\.{3,}\d+$` 或连续 `N. 标题 页码` 密集区，参考 `response_checker.py::_is_toc_region`），丢弃纯目录碎片；
     - **结构化章/表区块补召回（确定性 0-Token 增强）**：新增 `_collect_section_and_table_windows(factor, category, bid_text, bid_page_ctx)`：
       - 按评分类别/因子核心词（已清洗脏前缀）在非目录区定位章节标题（如 `业绩案例一览表`/`10.3`/`实施方案`/`售后服务`/`故障级别`/`质保期`/`项目进度计划`/`培训方案`），将该标题起连续的非目录正文段落 + 相邻带 `|` 分隔符的完整表格块（含表头+全表关键行）+ 紧随图片 OCR 块聚合成高质量结构化窗口（单窗口按**完整换行/句号边界**收口至 `~1400~1800` 字，总证据上限放宽至 `~4200` 字，禁止在句子中间切断引发模型“语句截断”幻觉）；
       - 每段证据经 `verify_quote()` 强制核验并按 `bid_page_ctx` 标定真实页码，去重后优先排在 BM25/L2 候选之前。
   - 修改 `_evaluate_criterion_with_llm` 提示词：明确告知模型「若正文包含业绩一览表、四级响应时间表或里程碑表，请直接依据表中具体数字与条文评分；不得因多段拼接边界断句而臆测标书正文未写完」。

### §2.2 `model_embedding` 配置通道与 `config/models.yaml`（打通可配置 L2 Rerank）

1. **`config/models.yaml`**：在 `envs.prod.models` 下显式补充注释占位（不改变默认实际生效值，防止当前无向量模型时发起无效 404 请求）：
   ```yaml
   # embedding: <可选：待公司内网网关上架向量模型后填入模型名，即可自动激活 L2 混合检索精排>
   ```
2. **后端用户模型配置透传（保持 14 张表不变，方案 A：同网关 `Base URL` + `API Key` 复用）**：
   - `backend/models.py`：`UserModelConfig`（第 9 表）新增 `model_embedding = Column(String(128), nullable=True)`，及老库平滑加列守卫 `ensure_user_model_configs_e44_columns`（同步挂入 `after_create` 与 `apply_sqlite_pragmas`）；
   - `backend/schemas.py`：`ModelConfigUpsertRequest` 与 `ModelConfigOut` 新增 `model_embedding: Optional[str] = Field(default=None, max_length=128)`（`ModelConfigOut` 默认 `''`），`ModelConfigTestOut` 新增 `embedding_ok: Optional[bool] = None` 与 `embedding_message: Optional[str] = None`；
   - `backend/routers/model_config.py`：读写持久化 `model_embedding`（留空/纯空白存 `None`，仅更新 `model_embedding` 不强制重填既有密钥）；`POST /api/model-config/test` 向 `build_client_from_user_config` 传入 `model_embedding`，并将非空 `model_embedding` 纳入 `/models` 检查且额外做一次轻量 `client.embed(['ping'])` 探活——若失败不阻塞 Chat 保存与主连通 `ok`，诚实返回 `embedding_ok=False` 与提示（检索将自动静默降级 L1 BM25）；
   - `backend/worker.py::resolve_effective_llm_client`：向 `build_client_from_user_config` 传入 `model_embedding=getattr(cfg, 'model_embedding', None)`（同时为 F4、F7、E42 激活 L2 混合精排）。
3. **前端 `ModelSettingsView.vue`**：
   - 在 `model_flash` / `model_flagship` 下方增加可选输入框「**向量精排模型 (`model_embedding` · 选填 · 用于 F4/F7/E42 混合精排 Rerank，留空自动 0-Token BM25)**」，直接共用上方 `Base URL` 与 `API Key`（不额外引入第二套 URL/Key，安全合规边界零复杂化）；
   - `PRESETS` 模板联动：`zhipu` 预填 `embedding-3`、`siliconflow` 预填 `BAAI/bge-m3`、`custom-intranet`/`deepseek`/`kimi`/`qwen` 默认 `''`（切模板时同步覆写 `form.model_embedding`，防止上一服务商向量模型名串用导致 404）；
   - 点击「测试连通性」时，若 `embedding_ok === false`，用清晰提示告知对话可用但向量不可用将降级 BM25。
   - **安全边界（写死）**：`model_embedding` 不引入第二套 URL/Key，向量请求与对话模型同源同锁——`STRICT_INNER_MODEL_ONLY=1` 时随主地址一并接受内网白名单校验，且 `embed()` 调用天然经 E40 网关三出口脱敏（标书正文分段送 `/embeddings` 与送 `/chat` 同等合规，绝无私网文本外泄公网通道）。

### §2.3 `core/price_extractor.py`（F2 《投标函》总价 vs 《开标一览表》交叉校验）

- 在 `core/price_extractor.py` 新增确定性辅助 `_extract_bid_letter_total(bid_text: str) -> Optional[dict[str, Any]]`：
  - 仅在正文出现《投标函》标志段落（如 `投标函` / `所附投标价格表中规定的应提交和交付的投标总价为人民币`）后匹配所填小写/大写投标总价，并严格跳过含 `最高限价`/`预算金额`/`保证金` 的干扰句；
  - 若提取到的有效《投标函》总价与 `bid_total`（均以 `Decimal` 量化 0.01 元）差额 > `0.00` 元，在 `run_price_check` 的 `findings` 中追加 `code='BID_LETTER_TOTAL_MISMATCH'`、`severity='warning'` 发现（如实标注《投标函》金额、开标一览表金额与差额，并附《政府采购货物和服务招标投标管理办法》87 号令第 59 条不一致修正提醒）。

### §2.4 全局跨页「任务完成提示通知 + 顶栏通知中心」（前端核心体验）

1. **Element Plus 组件与样式注册（严格遵循 `AGENTS.md` §三.7）**：
   - `frontend/src/main.ts`：引入 `import 'element-plus/es/components/notification/style/css'`；
   - `frontend/vite.config.ts`：`AutoImport` 解析器增补 `ElNotification`（并在调用处显式 `import { ElNotification } from 'element-plus/es/components/notification/index'`）；
   - `frontend/src/App.vue` 若使用 `<el-popover>` + `<el-badge>` 渲染顶栏通知中心，**白名单必须预列 `frontend/src/components.d.ts`**，构建时允许 `unplugin-vue-components` 自动补齐 `ElBadge` / `ElPopover` 声明。
2. **Pinia 全局任务追踪与通知 Store（`frontend/src/stores/notifications.ts`）**：
   - 管理两份按当前用户名/租户隔离的状态（内存 + `localStorage` 保留最近 20 条已完成历史，登出即停轮询）：
     - `trackedJobs`：当前正在后台跟踪的任务列表 `{ key, kind: 'check'|'score'|'composer'|'similarity'|'radar', targetId, title, status, startedAt }`；
     - `notifications`：到达终态的通知队列 `{ id, kind, targetId, title, summary, tone: 'success'|'warning'|'error', routePath, routeQuery, createdAt, read }`；
   - **跨页全局单定时器轮询（默认 2.5s 间隔，仅当 `trackedJobs.length > 0` 时运行，全部终态后自动停表零功耗）**：
     - 分别复用现有轻量化查询接口（`checksApi.detail` / `checksApi.getScoreEstimate` / `composerApi.getDraft` / `similarityApi.detail` / `radarApi.detail`），同时支持 `RadarView` 这类同步长请求 Promise 切页完成回调上报；
     - 对同一 `kind:targetId:updatedKey` 做幂等去重（防止详情页本地 SSE/轮询与全局通知对同一完成事件重复弹两条弹窗）；
     - 任务进入完成/待人工/失败终态时：
       1. 写入 `notifications`（未读角标 +1）；
       2. 在屏幕顶部右上角弹出 **`ElNotification`** 持久提醒（默认停留 6 秒可点关闭，附摘要如「检出 X 项高危 / 评分预估完成」），点击卡片直接 `router.push(...)` 跳转到对应任务详情/报告面板！
3. **五大耗时任务入口接线**：
   - `CheckDetailView.vue`（发起 `F2-F4 体检` 及 `E42 评分预估` 时自动 `registerTrackedJob`，当前页收到 SSE `done` / 评分完成时同步标为已处理防重弹）、`BidComposerView.vue`（`async_run` 生成草稿）、`SimilarityCheckView.vue`（`async_run` 交叉比对）、`RadarView.vue`（扫描/重扫）；
   - 顶栏 `.tb-right`（`App.vue`）放置「后台任务与完成通知」按钮（含在途运行数动态徽点 + 未读完成红点），弹出列表支持查看实时在途任务、历史完成消息、单条标已读/清空与一键直达。

### §2.5 文档同步
`HANDOVER.md`、`INTERFACE.md`（`bid_scorer.py` 签名与能力增强、`user_model_configs.model_embedding`、`ModelConfigUpsertRequest`/`ModelConfigOut`）、`FRONTEND.md`（顶栏通知中心与 `ModelSettingsView` 向量输入）、`AGENTS.md`（验收线更新）。

---

## 3. 验收标准

1. **单测覆盖（`tests/test_e44_scorer_precision_and_notifications.py`）**：
   - **Rerank 透传与降级**：Mock 含 `embed()` 与 `models={'flash':..., 'embedding':'bge-test'}` 的 `client` 跑 `estimate_bid_score`，断言 `_retrieve_verified_local_quotes` 成功激活 `level2_embedding_rerank`（`get_hybrid_stats()['level2_calls'] >= 1`）；而 `models` 无 `'embedding'` 时 0 次调用 `embed()` 静默回退 BM25；
   - **模型配置 `model_embedding` 全链路**：`PUT /api/model-config` 写入与 `GET` 回显 `model_embedding`、`resolve_effective_llm_client` 注入客户端、老库 `ensure_user_model_configs_e44_columns` 幂等补列（14 张表不变）；
   - **F2 跨处总价不一致**：构造《投标函》总价 `1400000` 与《开标一览表》`1395000` 的最小 `.docx` 用例（附招标限价 `140万元` 原文不误报），断言准确输出 `BID_LETTER_TOTAL_MISMATCH`；而 04 包一致场景零误报；
   - **E42 目录隔离 + 章表聚合 + OCR 缓存复用 + 脏因子名清洗**：构造含目录页行 `8. 实施方案\t150` + 后文完整表格与证书 `.media.ocr.json` 缓存的 `.docx`，断言目录行被剔除、ISO9001 识别加分、`items_sum == 100.00` 且 `score_scale_warning is None`。
2. **真标书实弹探针验收（点名 04 包 / 02 包，只读不写 `ground_truth/`）**：
   - 使用 `E:\agent\2.标书agent数据库，资料\` 下真实招标 PDF 与 `安易—04包-工业物性数据应用开发平台.docx` / `安易—02包.docx` 跑通评分细则抽取 + 局部证据聚合（单测通过环境检测跳过或走 Mock，**另行运行一次真语料验证命令并附实测输出摘要**）：
     - 04/02 包均稳定抽出 **8 项 = 100.00 分**（含价格 `30.00` 分），`score_scale_warning == None`；
     - 因子名无 `容清单...` / `商务要求（13 分）` / `政策分（2 分）` 粘连脏字；
     - `企业管理体系（3.00 分）` 命中 ISO9001 证书 OCR 图片，正确得分 **`1.00`**；
     - `相关业绩` 证据包含 `Table 5 业绩案例一览表` 与 `10.3` 合同 OCR 摘要，不再误滑到《投标函》；
     - 04 包 `项目进度计划` / `售后服务方案` / `实施方案` 证据分别命中 `Table 18`（`T+5`~`T+25`）、`Table 9`（1/4/8/24 小时响应）、正文章节（不再命中目录 `[P2]`）；
     - 02 包报价核验触发 `UPPER_MISMATCH` + `BID_LETTER_TOTAL_MISMATCH`（`1400000` vs `1395000`）。
3. **全量回归与构建**：
   - 全量 `.venv\Scripts\python.exe -m pytest tests/ -q` 零回归全绿；
   - `npm --prefix frontend run build`（`vue-tsc --noEmit && vite build`）零错误通过，检查 `components.d.ts` 仅含预期新登记组件且可正常工作。

---

## 4. 文件白名单

- `config/models.yaml`（仅 `prod.models` 下追加 `# embedding:` 注释占位）
- `core/bid_scorer.py`、`core/price_extractor.py`
- `backend/models.py`、`backend/schemas.py`、`backend/worker.py`、`backend/routers/model_config.py`
- `frontend/vite.config.ts`、`frontend/src/main.ts`、`frontend/src/components.d.ts`
- `frontend/src/api/types.ts`、`frontend/src/stores/notifications.ts`（新增）、`frontend/src/App.vue`
- `frontend/src/views/ModelSettingsView.vue`、`frontend/src/views/CheckDetailView.vue`、`frontend/src/views/BidComposerView.vue`、`frontend/src/views/SimilarityCheckView.vue`、`frontend/src/views/RadarView.vue`
- `tests/test_e44_scorer_precision_and_notifications.py`（新增）
- 文档：`HANDOVER.md`、`INTERFACE.md`、`FRONTEND.md`、`AGENTS.md`、`docs/cards/E44-E42评分精度精修与Rerank通道及全局通知卡.md`

---

## 5. 验收结果回执（2026-11-19 实测）

1. **单测与全量回归**：
   - 新增单测 `tests/test_e44_scorer_precision_and_notifications.py`：**`4 passed`**（覆盖 L2 Rerank `client` 透传激活与无向量 0 次调用降级、`user_model_configs.model_embedding` 14 表不变 + 老库平滑补列 + API 读写 + 非阻断 `/test` 探活 + `worker` 注入、F2 《投标函》跨处总价 `BID_LETTER_TOTAL_MISMATCH` 精准预警与 04 包零误报、PDF 跨行价格分 30 分补全 + 脏前缀清洗 + 目录隔离 + 整表聚合 + `.media.ocr.json` 缓存识别 ISO9001 得 `1.00/3.00`）；
   - E42 + E44 联合复验：`tests/test_e42_bid_score_estimation.py` + `tests/test_e44_scorer_precision_and_notifications.py`：**`8 passed in 17.91s`**；
   - 全量回归 `.venv\Scripts\python.exe -m pytest tests/ -q`：**`337 passed, 1 skipped`（共 338 项，0 失败，零回归**；唯一 skipped 为 E41 依赖旧外目录路径 `2.新agent数据法律` 的环境门禁探针）。
2. **北交大 04 包 / 02 包真实标书全图 OCR 实弹探针（隔离临时目录运行，零污染原目录与 `ground_truth/`）**：
   - **评分细则提取（硬伤 1 & 5）**：04 包与 02 包招标 PDF 均稳定抽出 **9 项（1 项价格分 `30.00` + 8 项非价格分 `70.00`）= `100.00` 分**，`score_scale_warning = None`；所有因子名干净无粘连脏字（`价格分`/`对招标文件中技术需求的响应程度`/`实施方案`/`售后服务方案`/`项目进度计划`/`培训方案`/`相关业绩`[`sec='商务要求'`]/`企业管理体系`/`节能环保`[`sec='政策分'`]），且 `售后服务方案`/`培训方案` 正确归类 `service`、`项目进度计划` 正确归类 `technical`。
   - **ISO9001 扫描件 OCR 出分（硬伤 2）**：04 包（109 图 OCR）与 02 包（91 图 OCR）的 `企业管理体系（3.00 分）` 均命中 `质量管理体系认证证书` 扫描件，正确判得 **`1.00 / 3.00`**，并如实提示缺失环境与职业健康安全管理体系证书。
   - **相关业绩 & 方案章表聚合 + 目录/投标函清单隔离（硬伤 3 & 4）**：两包 `相关业绩` 4 个检索窗口全命中实质业绩（W1 `【投标书内嵌表格 #6】` 湖南华辰 264.25 万 + 上海数腾 158.52 万，W2 `10.3业绩证明 / 10.3.1` 华辰合同 OCR，W3 `10.3.2` 数腾委托开发合同 OCR，W4 `10.3.3` 基层智治技术服务合同 OCR），零《投标函》清单滑偏与零目录行污染；04 包 `售后服务方案` 首位命中 `【投标书内嵌表格 #10】`（一级 1 小时响应）与 `【投标书内嵌表格 #17】`（2 年质保），`项目进度计划`/`实施方案` 首位命中 `【投标书内嵌表格 #19】`（`T+5日内`~`T+25日内`）与 `【投标书内嵌表格 #11】`，所有 `toc_tab=False`。
   - **F2 跨处总价一致性**：04 包 `status=OK, bid_total=470000.00, price_cap=500000.00, findings=[]`（零误报）；02 包 `status=OK, bid_total=1395000.00, price_cap=1400000.00`，精准触发 **`BID_LETTER_TOTAL_MISMATCH`**（`warning`，1400000.00 vs 1395000.00 差 5000.00 元，附财政部 87 号令第 59 条提醒）+ **`UPPER_MISMATCH`**（`error`）。
3. **前端类型检查与构建**：
   - `npm --prefix frontend run build`（`vue-tsc --noEmit && vite build`）：**0 错误通过，`1881 modules transformed`**；
   - `frontend/src/components.d.ts` 仅新增白名单预列的 `ElBadge` 与 `ElPopover` 两行声明。
4. **卫生核查**：
   - `git diff data/corpus/m0_samples/ground_truth/` 为空；`core/bid_scorer.py` 与 `core/price_extractor.py` 库函数零 `print(`；`data/cache` 无残留；`E:\agent\2.标书agent数据库，资料\` 无任何 `.ocr*` 残留文件；全部修改留在工作区待用户确认后 commit。
