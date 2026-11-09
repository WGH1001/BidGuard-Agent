# INTERFACE.md · BidGuard-Agent core↔backend 契约锁

> **本文档是 `core/` 引擎层与 `backend/` 服务层之间的唯一接口契约**。
> backend 层只允许通过本文登记的入口调用 core，禁止 import core 模块的 `_` 私有函数。
> 任何一侧要改本文登记的签名/返回结构/表结构，必须先改本文并登记变更记录，再动代码。

---

## 1. core 层对外入口契约（backend 唯一合法调用面）

> 以下签名与返回结构以 2026-11-05 基线（commit `1fb9ec6`）为准。
> **client=None 语义按模块分列，不得笼统推广**：F4/F6/F7（response_checker / rigging_radar /
> bid_react_agent）在 `client=None` 时执行 0 Token 确定性降级，绝不抛"缺模型"异常；
> **F1 的 `core/tender_llm.py` 例外**——其 `client=None` 表示"自动经 get_llm_client 加载全局网关"，
> 未配置 API Key 时会抛 RuntimeError，backend 降级模式下必须显式直调确定性层
> `build_review_checklist()`（见 §1.1）。

### 1.1 F1 招标红线提取（ExtractView / `/api/extract`）

```python
# core/tender_extractor.py —— 0 Token 确定性层
build_review_checklist(file_path: str) -> dict
# 返回：{'file','source_type','pages','chars','chapters','marked_clauses',
#        'invalid_bid_clauses','qualification_hits','compliance_hits',
#        'eval_chapter','scoring_hits','scoring_tables'}

# core/tender_llm.py —— LLM 结构化层（在确定性层结果上追加键）
extract_tender_with_llm(file_path: str, client=None, env: Optional[str] = None) -> dict
# = build_review_checklist 结果 + {'llm_structured': [...], 'llm_usage': {'calls', 'usage'}}

extract_scoring_with_llm(file_path: str, client=None, env: Optional[str] = None) -> dict
# 返回：{'scoring_tables': [...], 'scoring_prose': [...], 'scoring_llm_usage': {...}}
```

`marked_clauses` 元素结构（backend 存 `bid_findings` / 喂 F4/F5 时按此取字段）：
`{'no','title','marker','kind','start','end','text','quote','consequence','...'}`
—— `kind ∈ requirement / usage_note / table_marker`，**F4/F5/F6 消费端必须排除后两类**；
`consequence ∈ veto / deduction / bonus / none`。

### 1.2 F2 报价核验（CheckDetail / `/api/checks` 内嵌步骤）

```python
# core/price_extractor.py
run_price_check(bid_path: str | Path, tender_path: str | Path | None = None) -> dict
# 返回：extract_price_data 全部键 + 'findings'
#   {'status': 'OK'|'NEED_MANUAL'|..., 'items': [...], 'table_total', 'bid_total',
#    'bid_total_upper', 'price_cap', 'has_price_table_skeleton', 'findings': [...]}

# core/price_sandbox.py —— status=='OK' 时的四项算术核验（run_price_check 已封装）
PriceSandbox(items, table_total, bid_total, bid_total_upper, price_cap).run() -> list
# findings 元素：{'code', 'severity', 'message', **detail}；空列表 = 全部通过
```

**铁律**：`status=='NEED_MANUAL'` 时必须显式降级给前端人工回填弹窗，禁造数据；
`price_cap` 缺失时跳过限价核验，不视为失败。

### 1.3 F3 证照效期与串名（0 Token）

```python
# core/qualification_scanner.py
scan_qualifications(text: str, open_date: str,
                    required_social_months: list = None,
                    forbidden_names: list = None,
                    target_buyer: str = '') -> list
# text = 投标文件全文（parse_document(...).text）；open_date 形如 "2026-11-04"
# 返回 findings 列表（格式同 F2）；开标日不可解析时返回
# [{'code': 'INVALID_OPEN_DATE', 'severity': 'error', 'message': ...}]
```

### 1.4 F4 双书勾稽核验（CheckDetail 主流程）

```python
# core/response_checker.py
run_response_check(tender_path: str | Path, bid_path: str | Path,
                   requirements: Optional[list[dict]] = None,
                   client=None) -> dict[str, Any]
# 返回：{'findings': [...],            # MISSING_RESPONSE + CONTRADICTION + MISSING_EVIDENCE 汇总
#        'star_results': [...],        # 每条 ★/▲ 要求核验详情（含 RESPONSE_OK）
#        'self_contradictions': [...], # 投标书内部矛盾
#        'alignment_summary': {'total_requirements','aligned_hits','hit_rate',...}}
# requirements 传 None 时内部自动跑 build_review_checklist（重复解析，后端应复用 F1 结果传入）
```

`star_results` 与传入的 `requirements`（剔除 usage_note/table_marker 后）**严格同序**——
backend 组装偏离表/风险卡片时按下标对位，禁止按 quote 文本二次匹配。

### 1.5 F5 偏离表导出（DeviationView / `/api/deviation`）

```python
# core/deviation_exporter.py
export_deviation_table(tender_path: str | Path, bid_path: str | Path,
                       output_path: str | Path,
                       requirements: Optional[list[dict]] = None,
                       client=None) -> dict[str, Any]
# 返回：{'output_path', 'total_rows', 'no_deviation_count',
#        'negative_deviation_count', 'missing_count', 'contradiction_count',
#        'rows': [...], 'f4_result': dict}
```

### 1.6 F6 投前控标雷达（RadarView / `/api/radar`）

```python
# core/rigging_radar.py
scan_rigging_radar(tender_path: str | Path, client=None) -> dict[str, Any]
# 返回：{'suspicious_params': [{'quote','offset','exclusive_type','confidence',
#          'reason','quote_verified','llm_status'}, ...],
#        'conclusion': {'recommendation': '值得投'|'谨慎投'|'疑似控标高', 'summary'},
#        'llm_status': 'done'|'skipped'}
```

### 1.7 F7 标书专家追问（QaChatView / `/api/qa`）

```python
# core/bid_react_agent.py
ask_bid_expert(tender_path: str | Path, bid_path: str | Path, question: str,
               client=None, max_rounds: int = 6) -> dict[str, Any]
# 返回：{'mode': 'react'|'keyword_fallback', 'question', 'answer',
#        'quotes': [{'source','quote','offset','page','quote_verified'}, ...],
#        'steps': [...], 'rounds_used': int, 'terminated_by_max_rounds': bool}
# page：仅 PDF 有真实页码，docx 恒为 None，前端必须处理 null 展示
# 已挂载 6 工具：search_tender / search_bid / locate_chapter / recalc_price / check_cert_expiry / cite_regulation
```

### 1.8 批注回写与解析底座

```python
# core/parser.py
parse_document(file_path: str) -> ParsedDoc          # .text / .source_type / .pages
build_pdf_page_spans(file_path: str | Path | None) -> list[tuple[int, int, int]]
offset_to_page(offset: int | None, page_spans: list[tuple[int, int, int]]) -> int | None

# core/redline.py —— 原位标红批注（交付中心 ZIP 用）
inject_word_comments(src_docx_path: str, findings: list,
                     out_docx_path: str = None, author: str = 'AI法务初审员') -> bytes
inject_pdf_highlights(src_pdf_path: str, findings: list,
                      out_pdf_path: str = None, author: str = 'AI法务初审员') -> bytes
```

### 1.9 模型网关（含用户级自定义 Key）

```python
# core/llm_gateway.py
get_llm_client(env: Optional[str] = None) -> LLMClient      # 读 config/models.yaml
build_client_from_user_config(base_url: str, api_key: str, model_flash: str,
                              model_flagship: Optional[str] = None,
                              supports_reasoning_effort: bool = False) -> LLMClient
```

### 1.10 E4 招投标法规库与法条三态真伪核验（RulesAndCertsView / `/api/regulations`）

```python
# core/regulation_library.py
normalize_article_text(text: str) -> str
verify_article(text: str, session=None, client=None, fuzzy_threshold: float = 0.35) -> dict
# 返回：{'status': 'exact_match'|'fuzzy_hint'|'unverifiable', 'verified': bool,
#        'law': str|None, 'article_no': str|None, 'official_content': str|None,
#        'similarity': float, 'badge': str, 'message': str}
cite_regulation(keyword: str = '', article_no: str = '', law_name: str = '', session=None) -> dict
import_regulation_file(session, file_path: str | Path, law_name_hint: Optional[str] = None) -> dict

# core/regulation_crawler.py
crawl_all_configured_sources(session, yaml_path: Optional[str | Path] = None) -> dict
```

---

## 2. 数据库 11 张表规格（backend/models.py）

> SQLAlchemy 2.0 风格；PostgreSQL 16 主库，`DATABASE_URL` 为 sqlite 时无缝回退（开发/单测）。
> 所有表含 `id`（自增主键）、`created_at`；更新型表另含 `updated_at`。时间一律 UTC。

| # | 表名 | 职责 | 关键字段（除公共外） |
|---|------|------|----------------------|
| 1 | `tenants` | 多租户投标部门 | `name`（唯一）、`is_active` |
| 2 | `users` | 用户与角色鉴权 | `tenant_id` FK、`username`（**全局唯一** unique=True）、`password_hash`(bcrypt)、`display_name`、`role`(admin/member)、`is_active`、`last_login_at` |
| 3 | `bid_check_tasks` | 体检/控标任务主表 | `tenant_id`、`user_id`、`task_type`(pre_check/radar)、`title`、`status`、`progress`、`stage_detail`、`tender_filename`、`bid_filename`、`tender_path`、`bid_path`、`error_msg`、`stats_json`、`price_manual_json`、`config_json`、`llm_usage_json`、`delivered_at` |
| 4 | `bid_findings` | 检出风险明细 | `task_id` FK(级联删除)、`source_module`、`code`、`severity`、`category`、`clause_no`、`quote`、`bid_quote`、`quote_offset`、`quote_verified`、`page`、`message`、`suggestion`、`consequence`、`detail_json`、`feedback`、`feedback_note` |
| 5 | `bid_rule_configs` | 核查参数与自定义红线 | `tenant_id`、`name`、`price_cap`(Numeric 14,2)、`open_date`(Date)、`buyer_name`、`required_social_months_json`、`custom_redlines_json`、`is_default` |
| 6 | `company_certificates` | 企业证照效期台账 | `tenant_id`、`cert_name`、`cert_no`、`valid_until`(Date)、`warn_days`(默认 30)、`remark` |
| 7 | `qa_sessions` | F7 追问会话持久化 | `tenant_id`、`user_id`、`task_id`(可空 FK)、`title`、`tender_path`、`bid_path`、`messages_json` |
| 8 | `audit_logs` | 安全审计留痕 | `tenant_id`、`user_id`、`action`、`target`、`detail_json`、`ip` |
| 9 | `user_model_configs` | 用户级自定义模型配置 | `user_id`(唯一 FK)、`base_url`、`api_key_encrypted`、`model_flash`、`model_flagship`、`supports_reasoning_effort`(bool) |
| 10 | `regulation_laws` | E4 招投标法规主表 | `name`（唯一《...》全称）、`issuing_body`、`effective_date`、`source_url`、`article_count`、`content_hash`、`fetched_at`、`is_seed`(bool) |
| 11 | `regulation_articles` | E4 法规条文表 | `law_id` FK(级联删除)、`article_no`("第X条")、`article_title`(可空)、`content`(Text)、`content_hash`、`UNIQUE(law_id, article_no)` |

---

## 3. backend 层横切约定

- 路由列表（11 组）：`auth`、`dashboard`、`checks`、`extract`、`deviation`、`radar`、`qa`、`rules`、`reports`、`model_config`、`regulations`。
- 其余鉴权、多租户隔离、SSE 进度流、文件存储布局与统一错误格式约定保持不变。

---

## 4. 变更登记（追加，不删除）

| 日期 | 变更 | 登记人 |
|------|------|--------|
| 2026-11-05 | 初版：锁死 core 九类入口签名 + 9 表规格 + 横切约定 | 架构侧 |
| 2026-11-05 | B1 卡审前修订：① §1 降级声明按模块分列 ② users.username 改全局唯一 ③ bid_findings 增 detail_json 兜底列 ④ 令牌 payload 键名锁死 sub/tenant_id/role/exp | 架构侧 |
| 2026-11-05 | B1 验收修正：① users.username 落实全局唯一 ② create_token payload 仅留 sub/tenant_id/role/exp；保留 bid_quote 列与 BidFinding 只读属性 | 架构侧 |
| 2026-11-06 | E4 法规库与法条核验卡：① §1.10 新增 `core/regulation_library.py` 与 `core/regulation_crawler.py` 契约签名，F7 新增第 6 工具 `cite_regulation` ② §2 新增第 10、11 表 `regulation_laws` 与 `regulation_articles` ③ 挂载 `/api/regulations` 路由组 | 实现侧 |
| 2026-11-09 | E10 前端功能走查与并发稳定性卡：① `/api/radar` 补齐 `GET /api/radar`（历史列表）、`GET /api/radar/{task_id}`（回放 `RadarScanOut`）、`DELETE /api/radar/{task_id}` ② `DELETE /api/checks/{task_id}` 补齐运行中任务 `409 TASK_RUNNING` 拒删守卫与上传文件/导出产物级联清理 ③ `/api/regulations` 新增 `DELETE /api/regulations/laws/{law_id}`（`require_admin` 级联清理条文） ④ `/api/dashboard` 将 F1 提取条款数纳入 `defense_matrix` 统计 | 实现侧 |