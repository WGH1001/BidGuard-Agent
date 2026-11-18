# E41 · 双业务线专精 Skills 与常见指标标准应答库卡【领导标重点：业务 3 + 业务 5】

> 状态：已实现并全量验收（工作区待确认 commit）｜ 当前代码基线：E40 `377e37d` 已入库 → E41 验收完成（325 项 pytest 全绿 + `vue-tsc`/`vite build` 1865 模块零错误，14 表 / 13 组路由 / 15 视图）
> 领导需求映射：**业务 3「常见指标以及对应内容的自动化输出」【重点】+ 业务 5「不同业务线核心指标、评分标准的自动化输出与参考」**；业务 4 查重（E36 已落地）不再投入。
> 会上口径（2026-11-18 定）：公司首批两条业务线 = **①网络安全与数据安全 ②并行科技 MaaS 平台（Token/大模型/算力接入服务）**；实弹演示标的 = 安易 02 包（安全流转）与 04 包（知识库+模型接入）。

---

## 1. 核心目标

1. **把"泛泛而谈"变成"公司真实口径"**：F8 编制初稿时，按用户选定业务线自动注入**公司常见技术/商务指标的标准应答段落与参数表**（等保 2.0 二/三级响应、7×24/15 分钟响应/2 小时到场 SLA、Token 计费与配额管理、多模型网关路由、数据不出域保障等），让 02/04 包产出的初稿直接贴合公司技术口径与典型评分响应。
2. **架构零侵入**：新建 `core/indicator_library.py` 实现 F8 既有 `KnowledgeSnippetProvider` 协议（`retrieve(query_text, top_k, min_score)`）——**复用 `bid_composer.py` 现有 `knowledge_provider` 插槽**，撰写链路不新增旁路；预设 Skill 走 E38 既有 `BUILTIN_WRITING_SKILLS` 自动播种机制。
3. **指标库内容人工终审红线**：库文件为**数据资产**（`data/rules/indicator_library/`），由提炼脚本产**草稿**、人工（用户/领导）终审确认公司真实口径后才入库；**缺公司确认的 SLA 硬参数一律做成 `[待人工确认：...]` 参数槽位**，绝不编造。

---

## 2. 技术设计明细

### §2.1 新模块 `core/indicator_library.py`（0 Token 指标库检索与注入）
1. **`IndicatorEntry` 数据结构**：`{line: 'common'|'security'|'maas', category: '技术方案'|'SLA运维'|'安全资质'|'计费配额', indicator_name, standard_response, scoring_ref: {typical_points, weight_hint, high_score_tips}, param_slots: [...], keywords: [...], source_ref}`——**三层业务线**：`common` 为跨行业通用政采必考指标（售后 SLA/实施排期/培训文档/保密承诺/常规商务响应），`security`/`maas` 为业务线专精；**检索命中规则 = 所选业务线条目 + `common` 层叠加**（通用 Skill 也能命中常见指标）；`scoring_ref` 承载领导第 5 条"评分标准参考"（典型评分点/权重参考/高分应答提示）。
2. **`IndicatorLibraryProvider`**：实现 `bid_composer.KnowledgeSnippetProvider` 协议（`has_documents` / `retrieve(query_text, top_k, min_score)`）——检索用 **0-Token BM25/关键词三级门控**（复用 `hybrid_retriever` 门控思想，不进大模型），按 `line`（选中线 + common 叠加）过滤 + `keywords`/`category` 加权命中指标条目，返回片段带 `[公司标准应答参考｜{indicator_name}]` 前缀。
3. **防废标硬锁适配**（任何指标库内容不可突破）：
   - 库内条目**严禁含投标专用报价金额**——Token 单价/折扣等商务数字一律渲染为 `[待人工填报：Token 单价 ___ ￥/M Tokens]` 槽位（复用报价锁账语义）；
   - **公司已确认口径方可直书**：经核实的安易 04 包实测 SLA 为 `7×24 电话支持 / 5×8 支持 / 故障响应 ≤1 小时 / 到场 ≤24 小时 / 同规格代用`——仅此类有源可查的数字可作标准应答正文；**其余未经公司确认的（如 API 可用性 99.9%/TTFT/TPS/QPS/等保级数等，安易两包中均未出现）一律 `[待人工确认：...]` 参数槽位**；
   - 注入的片段属"公司自有知识"，不受 `verify_quote` 对招标原文的溯源约束，但撰写 prompt 须标注"仅供参考，需结合本项目实际改写"，且 `_sanitize_ai_draft_text` 照常拦截幻觉金额/合同编号。
4. **草稿提炼脚本 `scripts/build_indicator_library.py`**：从 `安易—02包.docx`、`安易—04包-*.docx` 与 `E:\agent\1.合同法律\公司测试合同\` 6 份 MaaS 合同模板确定性抽取候选指标段落（标题层级+关键词锚点，0 Token，产出草稿 JSON + 抽取率统计），**输出仅供人工终审，严禁直接落 `data/rules/` 正式库**。

### §2.2 数据资产 `data/rules/indicator_library/`（人工终审后入库）
- `common_indicators.json`（通用层：政采标书必考的售后 SLA / 实施交付与培训 / 安全保密 / 常规商务响应标准应答）；
- `security_indicators.json`（网络安全线：等保响应规范/渗透漏扫流程/应急 SLA/安全团队架构与保密承诺/磁盘加密防勒索/逻辑隔离访问控制——源自安易 02 包真实方案结构）；
- `maas_indicators.json`（MaaS 线：Token 计费结构与额度包/多模型网关与路由（DeepSeek/Qwen/GLM 系列 SKU 名与上下文规格，源自合同模板实测）/私有化推理与 GPU 调度/数据不出域承诺/知识产权与数据保密条款口径）；
- JSON 带 `schema_version` 与 `reviewed_at`/`reviewed_by` 字段（终审留痕），`content_hash` 幂等。

### §2.3 两套新内置预设 Skill（走 E38 播种机制，零迁移）
- `BUILTIN_WRITING_SKILLS` 新增：**`网络安全与数据安全专精版`**（tone 沿用 `technical_deep` + 等保/隔离/加密写作约束）、**`并行MaaS算力服务专精版`**（含 Token 计费表述规范与多模型路由写作约束）；`ensure_builtin_composer_skills` 自动对存量租户补种，老库无迁移。

### §2.4 F8 接线与前端
1. `backend/routers/composer.py`：`/generate` 新增 `use_indicator_library`（默认 `False`）；当所选 Skill `line != 'general'` 或开关开启时，构造 `IndicatorLibraryProvider` 传入 `compose_bid_blueprint` 的 `knowledge_provider` 插槽（与 V2 方案库 Provider 可叠加：指标片段优先、方案库片段互补）。**接线注意（实测 `bid_composer.py` L1308~L1312）**：现网仅 `compose_mode == 'knowledge_draft_v2'` 才触发 `knowledge_provider`——E41 必须放行指标库开启场景（`use_indicator_library=True` 或 Skill `line != 'general'` 时同样构造 Provider），不得只在 V2 模式生效；
2. `ComposerSkill` 模型加 `biz_line`（`general|security|maas`）字段 + 老库平滑加列（沿用 `ensure_*` 模式）；`/skills` 列表透出业务线徽标；
3. 前端 `BidComposerView.vue`：Skill 下拉按业务线分组徽标 + "引用公司指标标准应答库"开关（默认跟随 Skill 业务线自动开启）+ **"核心指标与评分标准参考"预览卡片**（选定业务线后直观可见该线 `scoring_ref` 典型评分点/权重参考/高分提示，可随报告导出）；组件实现只用已注册的 `el-option`/`el-tag`/现有组件组合，**不引入 `el-option-group` 等新组件，避免触碰 `components.d.ts` 白名单**；`types.ts` 同步。**涉 `types.ts`：本卡必须串行实现。**

### §2.5 文档同步
`HANDOVER.md`、`INTERFACE.md`（Provider 协议实现与 `/generate` 新参）、`FRONTEND.md`、`AGENTS.md`（如需沉淀"指标库人工终审"纪律）。

---

## 3. 验收标准

1. 单测：Provider 三级门控检索命中/不命中（**含 `common` 层叠加与跨业务线隔离**）、参数槽位渲染（库内禁报价金额）、`scoring_ref` 字段与预览数据透出、两预设自动播种、`/generate` 接线（`use_indicator_library` 开/关行为、**非 V2 模式下指标库仍生效**）、老库 `biz_line` 加列迁移；
2. 全量 pytest 零回归（当前线 321 项 + 本卡新增）+ `vue-tsc`/`vite build` 全绿；
3. **实弹探针（核心验收）**：以安易 04 包跑 F8 + MaaS 线 Skill + 指标库——初稿中 Token 计费/多模型路由/数据不出域章节引用标准应答且报价数字全部为槽位形态；以安易 02 包跑安全线——等保/隔离/加密章节同理；对比 E39 基线初稿体现"公司口径"增量；
4. 指标库 JSON 仅在用户/领导终审确认后入库（交付物含草稿 JSON + 终审清单模板）。

---

## 4. 文件白名单

- `core/indicator_library.py`（新增）、`core/composer_skills.py`、`core/bid_composer.py`（仅接线）
- `backend/models.py`、`backend/schemas.py`、`backend/routers/composer.py`
- `frontend/src/api/types.ts`、`frontend/src/api/index.ts`（验收补列 `composerApi`）、`frontend/src/views/BidComposerView.vue`
- `scripts/build_indicator_library.py`（新增）、`data/rules/indicator_library/common_indicators.json`、`data/rules/indicator_library/security_indicators.json`、`data/rules/indicator_library/maas_indicators.json`（新增，`review_status: pending_human_review`）
- `tests/test_e41_indicator_library_and_biz_skills.py`（新增）、`tests/test_e38_skills_ai_composer_and_async_similarity.py`（仅将 E38 旧内置数量 `== 3` 更新为基于 `BUILTIN_WRITING_SKILLS` 演进兼容）
- 文档：`HANDOVER.md`、`INTERFACE.md`、`FRONTEND.md`、`AGENTS.md`、`docs/cards/E41-双业务线专精Skills与常见指标库卡.md`

---

## 5. 验收记录

- 确定性抽提工具实跑（`.venv\Scripts\python.exe scripts/build_indicator_library.py`）：0 Token 扫描安易 02/04 包与 6 份并行科技 MaaS 合同模板共 8 份真实语料，安全落草稿至 `output/indicator_drafts/`（`common=12`、`security=11`、`maas=16` 候选命中，零正式库误写、零具体金额泄漏）；
- 专项与兼容性验收（`tests/test_e38_skills_ai_composer_and_async_similarity.py` + `tests/test_e41_indicator_library_and_biz_skills.py`）：**9 passed**（覆盖三层检索叠加与跨线隔离、`scoring_ref` 评分参考透出、老库 SQLite `biz_line` 平滑加列与 2 套新内置预设幂等补种、`GET /api/composer/indicators`、非 V2 `skeleton_v1` 模式指标库注入，以及安易 04 包/02 包真实招标 PDF 端到端出稿与槽位护栏）；
- 全量回归（`.venv\Scripts\python.exe -m pytest tests/ -q --tb=short`）：**325 passed, 1 warning in 419.67s**（321 基线 + E41 新增 4 项，零失败零跳过）；
- 前端全量校验（`npm --prefix frontend run build`）：**0 errors**，`1865 modules transformed`，`frontend/src/components.d.ts` 无新增或污染变更。
- 人工终审说明：正式库三份 JSON 当前标记 `review_status: "pending_human_review"`（`reviewed_at: null`），未证实 SLA（可用性 99.9%/TTFT/QPS/等保级数）及 Token 单价/折扣均留为 `[待人工确认：...]` / `[待人工填报：...]` 槽位；待业务负责人核定草稿清单 `output/indicator_drafts/REVIEW_CHECKLIST.md` 后即可填写签审留痕。
