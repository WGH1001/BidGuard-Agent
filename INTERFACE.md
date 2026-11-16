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
# 返回：{'file','source_type','pages','chars','open_date','open_date_source',
#        'open_date_quote','chapters','marked_clauses','invalid_bid_clauses',
#        'qualification_hits','compliance_hits','eval_chapter','scoring_hits',
#        'scoring_tables','required_certificates'}
extract_required_certificates(text: str, *, qualification_hits=None, compliance_hits=None,
                                marked_clauses=None, invalid_bid_clauses=None,
                                scoring_tables=None, scoring_prose=None, page_ctx=None) -> list[dict]
extract_open_date_from_tender_text(text: str) -> Optional[str]
extract_open_date_with_evidence(text: str) -> Optional[dict[str, Any]]
# 返回：{'open_date': 'YYYY-MM-DD', 'quote': 原文逐字片段, 'offset': int} 或 None
classify_invalid_clause_role(clause_text: str, clause_no: str = '') -> str
# E27：返回 'requirement' | 'eval_rule' | 'submission_rule'
extract_chapter4_procurement_requirements(text: str) -> list[dict]
# E27：无 ★/▲ 标书从第四章采购需求确定性提取技术/商务要求（clause_role='technical_or_commercial'）

# core/tender_llm.py —— LLM 结构化层（在确定性层结果上追加键）
extract_open_date_fallback_with_llm(text: str, client) -> Optional[dict]
# E23 规则未命中时局部窗（≤3 窗 ≤5000 字）LLM 兜底；强制过 verify_quote + 日期一致性校验，
# 返回 {'open_date': 'YYYY-MM-DD', 'quote': str, 'quote_verified': True, 'source': 'tender_llm', ...} 或 None
extract_tender_with_llm(file_path: str, client=None, env: Optional[str] = None,
                            *, open_date_fallback: bool = False,
                            progress_cb: Optional[Callable[[int, int], None]] = None) -> dict
# = build_review_checklist 结果 + {'llm_structured': [...], 'llm_usage': {'calls', 'usage'}}（open_date_fallback=True 时正则未命中则触发局部窗 LLM 开标日兜底；E30 新增可选 progress_cb 供 F1 逐条款心跳与 BaseException 取消透传）

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
extract_price_cap(text: str, package_no: int | None = None) -> str | None
extract_price_data(file_path: str | Path, tender_path: str | Path | None = None,
                   parsed_doc=None, tender_parsed_doc=None, client: Any = None) -> dict
run_price_check(bid_path: str | Path, tender_path: str | Path | None = None,
                parsed_doc=None, tender_parsed_doc=None, client: Any = None) -> dict
# E27：package_no 支持按目标包号提取分包预算限价；parsed_doc/tender_parsed_doc 复用 Stage 1.5 预解析结果
# E29：client!=None 时仅在确定性提取为 NEED_MANUAL（且无歧义预警）触发 _llm_perceive_price_tables，
#      LLM 仅返回单元格/行坐标，由 Python 回读原格并经 verify_quote + Decimal 重解析后再进 PriceSandbox；
#      同时增补 '合计' 列头识别及《开标一览表》横向合并表头跨列多行（人民币小写/大写）提取；
#      PriceSandbox._normalize_upper 对以 '元' 结尾省写 '整' 的写法自动补 '整'，连 '元' 都缺则保留 UPPER_MISMATCH
# 返回：extract_price_data 全部键 + 'findings'
#   {'status': 'OK'|'NEED_MANUAL'|..., 'items': [...], 'table_total', 'bid_total',
#    'bid_total_upper', 'reference_bid_total', 'price_cap', 'has_price_table_skeleton',
#    'llm_status': 'done'|'skipped', 'findings': [...]}

# core/price_sandbox.py —— status=='OK' 时的四项算术核验（run_price_check 已封装）
PriceSandbox(items, table_total, bid_total, bid_total_upper, price_cap).run() -> list
# findings 元素：{'code', 'severity', 'message', **detail}；空列表 = 全部通过
```

**铁律**：`status=='NEED_MANUAL'` 时必须显式降级给前端人工回填弹窗，禁造数据；
`price_cap` 缺失时跳过限价核验，不视为失败。

### 1.3 F3 证照效期与串名（0 Token）

```python
# core/qualification_scanner.py
resolve_effective_social_requirement(tender_text: str, open_date_str: str,
                                     manual_months: Optional[list[str]] = None,
                                     rule_months: Optional[list[str]] = None,
                                     rule_open_date: Optional[str] = None, *,
                                     configured_months: Optional[list[str]] = None,
                                     is_user_explicit: bool = False) -> tuple[list[str], Optional[int]]
scan_qualifications(text: str, open_date: str,
                    required_social_months: list = None,
                    forbidden_names: list = None,
                    target_buyer: str = '', *,
                    client: Any = None,
                    social_min_count: Optional[int] = None) -> list
# E27：开标日/年限动态推导失效默认社保月；局部社保窗保底 + 可选 client 语义鉴别（强制过 verify_quote）
# text = 投标文件全文（parse_document(...).text）；open_date 形如 "2026-11-04"
# 返回 findings 列表（格式同 F2）；开标日不可解析时返回
# [{'code': 'INVALID_OPEN_DATE', 'severity': 'error', 'message': ...}]
match_required_certificates_with_ledger(required_certs: list[dict], ledger_certs: list,
                                        reference_date: Optional[str] = None) -> tuple[list[dict], dict]
# E26：0-Token 将招标所需证照清单与租户 CompanyCertificate 台账保守对账，返回 (enriched_items, cert_ledger_summary)
```

### 1.4 F4 双书勾稽核验（CheckDetail 主流程）

```python
# core/response_checker.py
run_response_check(tender_path: str | Path, bid_path: str | Path,
                   requirements: Optional[list[dict]] = None,
                   client=None, *,
                   tender_parsed_doc: Any = None,
                   bid_parsed_doc: Any = None,
                   progress_cb: Optional[Callable[[int, int], None]] = None) -> dict[str, Any]
# check_star_responses(..., progress_cb=None) 同步接收可选 progress_cb；
# E30：Stage A/B 均支持 progress_cb 心跳与 BaseException 取消透传，阶段 B 并发池捕获 BaseException 后立即 pool.shutdown(wait=False, cancel_futures=True) 快速退出
# E27：submission_rule 在 F4 降级 severity='info', consequence='none'；在 F5 偏离表导出中默认过滤（include_submission_rules=False）
# E27：check_bid_self_contradictions 支持同句复合质保合并与未填模板下划线提示，L651 client.chat 异常单条降级不中断流水线
# E29：透传 ParsedDoc.media_ocr_spans；F4 定位与自相矛盾跳过目录页（\t页码）、内嵌历史合同/审计图片 OCR 区间，
#      并遍历全部出现位置对《技术偏离表》/正面响应窗口评分择优（短标题顺延截取连续响应句）
# 返回：{'findings': [...],            # MISSING_RESPONSE + CONTRADICTION + MISSING_EVIDENCE 汇总
#        'star_results': [...],        # 每条 ★/▲ 要求核验详情（含 RESPONSE_OK）
#        'self_contradictions': [...], # 投标书内部矛盾
#        'alignment_summary': {'total_requirements','aligned_hits','hit_rate',...}}
# requirements 传 None 时内部自动跑 build_review_checklist（重复解析，后端应复用 F1 结果传入）
# E19 起：阶段 A 严格单线程按序定位与写入向量缓存；阶段 B 仅 client.chat 进线程池并发
# （线程数由环境变量 BIDGUARD_F4_CONCURRENCY 控制，默认 4，设 1 回退纯串行），单条异常降级为 error_fallback。
```

`star_results` 与传入的 `requirements`（剔除 usage_note/table_marker 后）**严格同序**——
backend 组装偏离表/风险卡片时按下标对位，禁止按 quote 文本二次匹配。

### 1.5 F5 偏离表导出（DeviationView / `/api/deviation`）

```python
# core/deviation_exporter.py
export_deviation_table(tender_path: str | Path, bid_path: str | Path,
                       output_path: str | Path,
                       requirements: Optional[list[dict]] = None,
                       client=None,
                       *,
                       f4_result: Optional[dict[str, Any]] = None) -> dict[str, Any]
# 返回：{'output_path', 'total_rows', 'no_deviation_count',
#        'negative_deviation_count', 'missing_count', 'contradiction_count',
#        'rows': [...], 'f4_result': dict}
# E27 §2.6：传入已算好的 f4_result 时，内部按 valid_reqs 对齐并自动剔除 submission_rule，
#           实现 backend 流水线预生成与二次导出 0 额外 LLM 复用。
```

### 1.6 F6 投前控标雷达（RadarView / `/api/radar`）

```python
# core/rigging_radar.py
scan_rigging_radar(tender_path: str | Path, client=None, *,
                   progress_cb: Optional[Callable[[int, int], None]] = None) -> dict[str, Any]
# 返回：{'suspicious_params': [{'quote','offset','page','footer_page','is_docx_page',
#          'exclusive_type','confidence','reason','quote_verified','llm_status'}, ...],
#        'conclusion': {'recommendation': '值得投'|'谨慎投'|'疑似控标高', 'summary'},
#        'llm_status': 'done'|'skipped'}
# E30：candidates 循环起止透传 progress_cb(done, total)，供 radar.py 接入服务端 _cancel_flags 检查点
```

### 1.7 F7 标书专家追问（QaChatView / `/api/qa`）

```python
# core/bid_react_agent.py
ask_bid_expert(tender_path: str | Path, bid_path: str | Path, question: str,
               client=None, max_rounds: int = 7,
               reasoning_effort: Optional[str] = None,
               history: Optional[list[dict[str, Any]]] = None,
               attachments: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]

ask_bid_expert_stream(tender_path: str | Path, bid_path: str | Path, question: str,
                      client=None, max_rounds: int = 7,
                      reasoning_effort: Optional[str] = None,
                      history: Optional[list[dict[str, Any]]] = None,
                      attachments: Optional[list[dict[str, Any]]] = None) -> Iterator[dict[str, Any]]
# 流式事件帧：{'type': 'step'|'answer_delta'|'done'|'error', 'data': ...}；ask_bid_expert 消费其 done 帧保持兼容
# 返回（done 帧 data / ask_bid_expert）：
#        {'mode': 'react'|'keyword_fallback', 'question', 'answer',
#         'quotes': [{'source','quote','offset','page','footer_page','is_docx_page','quote_verified','attachment_id','attachment_name'}, ...],
#         'steps': [...], 'rounds_used': int, 'terminated_by_max_rounds': bool}
# E18 起：quotes/检索工具通过统一双轨页码上下文适配 PDF 与 docx；无法计算页码时诚实为 None 并展示字符偏移
# E19/E21 起：UI reasoning_effort 支持 'low' | 'medium' | 'high'，网关侧映射智谱合法值
#           （low->'low', medium->'high', high->'max'；三挡均开放至 7 轮=6 工具+1 final，仅调推理深度；非法值忽略走默认）
# E22 起：优先调用 client.translate_reasoning_effort() 读取 models.yaml chat_options.reasoning_effort_map 覆盖映射，
#         返回 None 时安全回退上述内置映射
# E21 起：history 注入多轮对话（最近 6 轮 / 单条 ≤800 字截断），0-Token 降级同步回溯历史提问关键词；
#         backend 新增 SSE 端点 POST /api/qa/sessions/{session_id}/ask/stream（done 帧原子落库）
# E24 起：已挂载 7 工具（search_tender / search_bid / locate_chapter / recalc_price / check_cert_expiry / cite_regulation / search_attachment）；
#         search_tender / search_bid / search_attachment / locate_chapter 新增可选 after_offset: int = 0；
#         新增 POST/DELETE /api/qa/sessions/{session_id}/attachments* 与单文件 20MB/单会话 10 个且 100MB/单轮 4 图管控；
#         纯视觉观察引用保留正文并显式标 quote_verified=False；_parse_llm_json(strict=False) 容错未转义换行且禁吐残缺工具 JSON
# search_tender(..., page_spans: list[...] | dict | None = None, after_offset: int = 0) / search_bid(...) 兼容旧版 list 与 E18 双轨 ctx dict
# E27 起：新增 DELETE /api/qa/sessions/{session_id}/last-turn（及隐藏别名 /{session_id}/last-turn）返回 QaLastTurnWithdrawOut；
#         POST /api/qa/sessions/{session_id}/ask 在配置网关失败（client is not None）时返回 HTTP 502 QA_GATEWAY_ERROR（不落库污染历史）
# E28 起：新增 PATCH /api/qa/sessions/{session_id}（及隐藏别名 /{session_id}，QaSessionRenameRequest）支持会话重命名与审计（qa_rename_session）；
#         _persist_qa_turn_locked 首问自动命名扩展覆盖空标题及 "{task.title} · 专家追问" 默认占位标题
```

### 1.8 批注回写与解析底座

```python
# core/parser.py
parse_document(file_path: str, *, ocr_images: bool = True,
               progress_cb: Optional[Callable[[int, int, int], None]] = None) -> ParsedDoc          # .text / .source_type / .pages / .media_ocr_spans
# E27：支持 Stage 1.5 统一预解析 progress_cb 实时回调（docx 每 10 张 .media.ocr.json 与扫描 PDF 每 5 页 .ocr.json 增量落盘及断点续扫）；ocr_images 保持向后兼容
# E29：.docx 内嵌图片 OCR 文本同步记录精确区间 media_ocr_spans=[{'start','end','paragraph_index'}]，PDF/TXT/图片默认 []
parse_qa_attachment(file_path: str | Path) -> dict    # E24: 低门槛 F7 附件解析（0~99 字图片不报错，返回 is_image/ocr_chars/has_text）
build_pdf_page_spans(file_path: str | Path | None) -> list[tuple[int, int, int]]
offset_to_page(offset: int | None, page_spans: list[tuple[int, int, int]]) -> int | None
build_docx_page_spans(file_path: str | Path | None) -> list[tuple[int, int, int, int | None]]
offset_to_docx_page(offset: int | None, spans: list[tuple[int, int, int, int | None]]) -> dict | None

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
                              supports_reasoning_effort: bool = False,
                              model_embedding: Optional[str] = None,
                              supports_vision: bool = False) -> LLMClient
encode_image_for_vlm(file_path: str | Path, max_edge: int = 2048,
                     target_bytes: int = 2 * 1024 * 1024) -> dict[str, Any]
# LLMClient E22/E24 新增方法与防御机制：
#   client.translate_reasoning_effort(value: str) -> Optional[str]  # 读 chat_options.reasoning_effort_map，未配置返 None
#   单实例内存熔断：chat() / chat_stream() 遇思考参数 400 拒绝时自动剥参重试并置 supports_reasoning_effort=False；
#                   遇上游拒绝视觉 image_url 输入时自动剥图重试并置 supports_vision=False（_mark_vision_unsupported）；
#   _build_extra_body() 无条件剔除 max_tokens 与 reasoning_effort_map 绝不透传上游
#   _estimate_messages_prompt_chars() 对 image_url 按固定 512 字符估算，严禁 Base64 长度 // 2 虚高 token
#   E27：LLMClient 读取 env_config['timeout']（默认 60.0s）并设 OpenAI max_retries=0 快速失败；
#        私有 IP 网关异常自动附带 TUN/代理路由排查提示（_format_gateway_error_hint）
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
| 3 | `bid_check_tasks` | 体检/控标任务主表 | `tenant_id`、`user_id`、`task_type`(pre_check/radar)、`title`、`status`、`progress`、`stage_detail`、`tender_filename`、`bid_filename`、`tender_path`、`bid_path`、`error_msg`、`stats_json`、`price_manual_json`、`config_json`、`llm_usage_json`、`started_at`、`finished_at`、`duration_json`、`delivered_at` |
| 4 | `bid_findings` | 检出风险明细 | `task_id` FK(级联删除)、`source_module`、`code`、`severity`、`category`、`clause_no`、`quote`、`bid_quote`、`quote_offset`、`quote_verified`、`page`、`message`、`suggestion`、`consequence`、`detail_json`、`feedback`、`feedback_note` |
| 5 | `bid_rule_configs` | 核查参数与自定义红线 | `tenant_id`、`name`、`price_cap`(Numeric 14,2)、`open_date`(Date)、`buyer_name`、`required_social_months_json`、`custom_redlines_json`、`is_default` |
| 6 | `company_certificates` | 企业证照效期台账 | `tenant_id`、`cert_name`、`cert_no`、`valid_until`(Date)、`warn_days`(默认 30)、`remark`、`attachment_name`、`attachment_path`、`attachment_size` |
| 7 | `qa_sessions` | F7 追问会话持久化 | `tenant_id`、`user_id`、`task_id`(可空 FK)、`title`、`tender_path`、`bid_path`、`messages_json`、`attachments_json` |
| 8 | `audit_logs` | 安全审计留痕 | `tenant_id`、`user_id`、`action`、`target`、`detail_json`、`ip` |
| 9 | `user_model_configs` | 用户级自定义模型配置 | `user_id`(唯一 FK)、`base_url`、`api_key_encrypted`、`model_flash`、`model_flagship`、`supports_reasoning_effort`(bool)、`supports_vision`(bool) |
| 10 | `regulation_laws` | E4 招投标法规主表 | `name`（唯一《...》全称）、`issuing_body`、`effective_date`、`source_url`、`article_count`、`content_hash`、`fetched_at`、`is_seed`(bool) |
| 11 | `regulation_articles` | E4 法规条文表 | `law_id` FK(级联删除)、`article_no`("第X条")、`article_title`(可空)、`content`(Text)、`content_hash`、`UNIQUE(law_id, article_no)` |

---

## 3. backend 层横切约定

- 路由列表（11 组）：`auth`、`dashboard`、`checks`、`extract`、`deviation`、`radar`、`qa`、`rules`、`reports`、`model_config`、`regulations`。
- E27 `duration_record` 新增 Stage 1.5 预解析耗时字段 `f_parse_ms`（保留 E11 既有 6 键 `f1_ms`/`f2_ms`/`f3_ms`/`f4_ms`/`grading_ms`/`total_ms`）；
- E30 协作式取消与活性心跳：`POST /api/checks/{task_id}/cancel`（`200` / `404` / `409 TASK_NOT_RUNNING`）、`BidTaskWorker._cancel_flags`（`_PipelineCancelled(BaseException)` / `PipelineCancelled` 统一穿透 core 降级块，连同 `POST /api/radar` 与 `/rescan` 消灭僵尸 `checking`）、SSE `progress`/`done`/`error` 帧携带 `updated_at`、`DELETE /api/checks/{task_id}` 对取消收尾中任务直接清理且运行中报错文案为 `"运行中任务请先中断后再删除"`，以及服务启动钩子 `recover_stale_tasks(session_factory=None) -> int`；
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
| 2026-11-09 | E12 L2 混合检索激活：`build_client_from_user_config` 增加 `model_embedding: Optional[str] = None`、`LLMClient.embed()` 接入 L2 重排 | 架构侧 + 复查 |
| 2026-11-10 | E11 五阶段耗时统计卡：`bid_check_tasks` 新增 `started_at`/`finished_at`/`duration_json` 三列及方言感知平滑迁移 `ensure_bid_check_tasks_columns`；SSE progress 帧透传 `started_at` | 实现侧 + 验收 |
| 2026-11-10 | E15/E18 双轨页码大一统：`core/parser.py` `build_docx_page_spans`/`offset_to_docx_page` 联动统一页码上下文覆盖 F1/F2/F3/F4/F5/F6/F7；`search_tender`/`search_bid` 第 4 形参兼容 `list \| dict`；开标日期缺省自动正则回填 | 实现侧 + 验收 |
| 2026-11-10 | E16 交付中心升级：`/api/reports` 新增 `GET /api/reports/{task_id}/signoff`（终核签字单下载）与 `POST /api/reports/batch-zip`（多任务内容自选批量打包） | 实现侧 + 验收 |
| 2026-11-10 | E19 UX 体验修复与 F4 并发提速：`core/response_checker.py` 升级两阶段并发（受 `BIDGUARD_F4_CONCURRENCY` 环境变量控制，默认 4）；`ask_bid_expert` 与 `QaAskRequest`（`POST /api/qa/sessions/{id}/ask`）新增可选参数 `reasoning_effort`（`'low'\|'medium'\|'high'`） | 实现侧 + 交叉复查 |
| 2026-11-11 | E20 F1/F5/F6 体验与 Docker 端口共存：仅前端视图与 `docker-compose.yml`（`POSTGRES_PORT:-15432`）调整，core↔backend 签名零改动 | 实现侧 + 验收 |
| 2026-11-11 | E21 F7 多轮上下文与流式直播：`core/bid_react_agent.py` 新增 `history` 参数与流式入口 `ask_bid_expert_stream`，三挡推理映射智谱合法值 `low/high/max`（`max_rounds` 默认放开至 `7` 轮以支持 6 工具+1 `final`，仅调深度）；`backend/routers/qa.py` 同步与流式端点注入 `messages_json` 历史并新增 `POST /api/qa/sessions/{id}/ask/stream`（`QaAskRequest.max_rounds` 放宽至 `1..8` 默认 `7`） | 实现侧 + 验收修复 |
| 2026-11-11 | E22 LLM 网关思考参数防御加固：`LLMClient` 新增 `translate_reasoning_effort(value)`，`_is_thinking_reject` 升级为组合条件识别，`_build_extra_body()` 强制隔离 `reasoning_effort_map`，`chat()` 与 `chat_stream()` 共用剥参重试与单实例内存熔断（`_mark_effort_unsupported`）；`bid_react_agent._resolve_effort_and_rounds(..., client=None)` 向后兼容新增可选 `client` 形参 | 实现侧 + 验收 |
| 2026-11-12 | E23 开标日期全链路与工作区/工具修复：`core/tender_extractor.py` 新增通用 `extract_open_date_from_tender_text` / `extract_open_date_with_evidence` 并纳入 `build_review_checklist` 返回 `'open_date'` / `'open_date_source'` / `'open_date_quote'`；`core/tender_llm.py` 新增局部窗兜底 `extract_open_date_fallback_with_llm`（严格过 `verify_quote` + 日期一致性校验）与 `extract_tender_with_llm(..., *, open_date_fallback=False)`；`/api/extract`（`ExtractChecklistOut`）与 `/api/radar`（`RadarScanOut`）透出 `open_date` / `open_date_source` / `open_date_quote`；`POST /api/checks` 新增手填开标日严格 `YYYY-MM-DD` 日历校验（`HTTP 422 INVALID_OPEN_DATE`）；`backend/worker.py` 持久化 `open_date_source` 并在手填≠标书识别日时发出 `OPEN_DATE_MISMATCH` 预警；`core/bid_react_agent.py` 新增 `_coerce_action_input` 自动解包字符串化 JSON 并完善 `recalc_price` 缺失 `total_price` 校验提示 | 架构侧 + 验收 |
| 2026-11-12 | E24 F7 多文件与图片附件问答：`core/parser.py` 新增 `parse_qa_attachment` 低门槛解析入口（取消 `OCR_MIN_CHARS` 报错，`.ocr.txt` 缓存，保留 `parse_document` 不变）；`core/llm_gateway.py` 新增 `encode_image_for_vlm`（PyMuPDF `Pixmap` 真实像素探测 + 缩放降字节）、`supports_vision` 传参/单实例拒图熔断（`_is_vision_reject` / `_strip_image_blocks` / `_mark_vision_unsupported`）及图像占位 token 估算；`backend/models.py` 扩展 `qa_sessions.attachments_json` 与 `user_model_configs.supports_vision` 及方言感知迁移守卫（含 SQLite PRAGMA 启动自动检测）；`backend/routers/qa.py` + `schemas.py` 新增 `/attachments` 上传删除与配额校验（20MB/10个/100MB/单轮≤4图/INVALID_ATTACHMENT_ID）、级联删盘、`QaAttachmentOut`/`QaAskRequest`/`QaQuoteOut` 扩展；`backend/routers/model_config.py` + `worker.py` 全链路透传 `supports_vision`；`core/bid_react_agent.py` 扩至 7 工具（新增 `search_attachment`）、仅当轮附 `image_url`、`_verify_and_locate_quotes` 附件强制溯源/纯图降级、`_parse_llm_json(strict=False)` 容错、禁吐工具 JSON 与 `after_offset` 续检 | 实现侧 + 验收 |
| 2026-11-12 | E25 全链路工作流与台账运维补强：`backend/models.py` 为 `company_certificates` 新增 `attachment_name`/`attachment_path`/`attachment_size` 三列及 `ensure_company_certificates_columns` 方言迁移守卫；`backend/routers/deviation.py` 增加运行中任务 `409 CHECK_TASK_RUNNING` 拦截，`checks.py`/`worker.py` 重跑清理旧偏离表 sidecar；`radar.py` 新增 `POST /api/radar/{task_id}/rescan`；`qa.py` + `QaSessionOut` 新增 0-Token 上下文动态 `suggested_questions`；`regulations.py` 新增 `DELETE /api/regulations/articles/{article_id}`；`reports.py` 新增 `GET /api/reports/audit-logs/export`（UTF-8 BOM CSV + 公式注入转义）；`rules.py` 新增 `parse-file` OCR 预填与 `{cert_id}/attachment` 上传下载删除闭环 | 架构侧 + 验收 |
| 2026-11-12 | E26 招标所需证照清单与台账联动：`core/tender_extractor.py` 新增 0-Token `extract_required_certificates` 并纳入 `build_review_checklist` 返回 `'required_certificates'`；`core/qualification_scanner.py` 新增 `match_required_certificates_with_ledger`；`backend/schemas.py` 新增 `MatchedLedgerCertOut`/`RequiredCertItemOut`/`CertLedgerSummaryOut` 并扩展 `ExtractChecklistOut`；`backend/routers/extract.py` 按当前租户查询 `CompanyCertificate` 执行四态对账并返回台账匹配汇总 | 架构侧 + 验收 |
| 2026-11-13 | E27 真标书实战排障与交互体验深化：`core/parser.py` `parse_document` 新增可选 `ocr_images`/`progress_cb` 与 `.docx` 每 10 张 `.media.ocr.json` / 扫描 `.pdf` 每 5 页 `.ocr.json`（`partial=True` 断点续扫与原子落盘）；`core/price_extractor.py` `extract_price_cap` 新增 `package_no` 分包预算精准匹配并支持复用 `parsed_doc`/`tender_parsed_doc`；`core/qualification_scanner.py` 新增 `resolve_effective_social_requirement` 并扩展 `scan_qualifications(..., client=None, social_min_count=None)`（局部社保窗隔离 + 受控 LLM 语义鉴别）；`core/tender_extractor.py` 新增 `classify_invalid_clause_role`（§2.6 扩展 `_EVAL_COMMITTEE_RULE_RE` 兼容 `6、报价过低` 及残片）与 `extract_chapter4_procurement_requirements`（§2.6 新增 `_pick_procurement_chapter` 跳过前文短目录存根、`（1）/(1)/1.` 编号条支持及 Pattern B 收缩先于 600 字截断）；`core/response_checker.py` 区分 `eval_rule`/`submission_rule`（F4 降级 `info`+`none`，F5 默认过滤）、复合质保合并与 L651 `try/except` 单条降级；`core/deviation_exporter.py` `export_deviation_table` 新增 keyword-only `f4_result=None` 支持 0 额外 LLM 复用导出；`core/llm_gateway.py` 新增 `timeout`/`max_retries=0` 与私有 IP TUN 诊断提示；`backend/worker.py` 拆分 Stage 1.5 `f_parse_ms` 耗时与冷启动 `cached_hits=0` SSE 进度修正，并在 Stage 4 前调用 `build_and_cache_deviation_artifacts`（复用 `f4_result` 预生成 F5 docx 与 `_stats.json` 快照，同步给 `deviation.py`/`reports.py` 复用）；`backend/routers/qa.py` 新增 `DELETE /api/qa/sessions/{id}/last-turn` 及 `/ask` 502 `QA_GATEWAY_ERROR` 隔离 | 架构侧 + 实现 |
| 2026-11-16 | E28 F7 多会话并发流与重命名/首问自动命名补强：`backend/models.py` SQLite 新增 `PRAGMA busy_timeout=5000`；`backend/schemas.py` 新增 `QaSessionRenameRequest`；`backend/routers/qa.py` 新增 `_QA_DB_WRITE_LOCK` / `_persist_qa_turn_locked` 写锁串行化与 `PATCH /api/qa/sessions/{session_id}`（含隐藏别名与 `qa_rename_session` 审计），并将 `_is_default_session_title` 扩展到 `"{task.title} · 专家追问"` 占位标题的首问自动重命名；前端 `QaChatView.vue` 多会话 `cachedRuntimes` Map + Pinned/Projects 树形侧栏 + 行内/顶栏双击重命名及 `ExtractView.vue` F1 概览条通栏+Grid 布局对齐 | 架构侧 + 实现 |
| 2026-11-16 | E29 感知层升级与真标书 F2/F4/OCR 实战排障：`core/parser.py` 为 `ParsedDoc` 挂载内嵌图片 OCR 精确区间 `media_ocr_spans`；`core/response_checker.py` 新增目录区/OCR 历史合同隔离、全命中评分择优与短标题顺延截取；`core/price_sandbox.py` `_normalize_upper` 补全以 `元` 结尾省写 `整` 的写法；`core/price_extractor.py` 增补 `'合计'` 列、近 3 段表前标题、《开标一览表》横向合并表头跨列多行子标签取值，及 `NEED_MANUAL` 时 LLM 单元格坐标感知 + Python `verify_quote`/`Decimal` 重解析兜底；`backend/worker.py` 修复 Stage 1.5 全缓存/零帧 OCR 播报、透传 `client=client` 与 `stats_payload` 报价预填字段；前端 `PriceManualDialog.vue`/`CheckDetailView.vue`/`types.ts` 清除硬编码假数据改用真实预填 | 实现 |
| 2026-11-16 | E30 可中断流水线与活性心跳卡：`core/tender_llm.py` `extract_tender_with_llm`、`core/response_checker.py` `check_star_responses`/`run_response_check` 与 `core/rigging_radar.py` `scan_rigging_radar` 新增可选 `progress_cb: Optional[Callable[[int, int], None]] = None` 并透传取消 Signal（`response_checker` 并发池捕获 `BaseException` 立即 `pool.shutdown(wait=False, cancel_futures=True)`）；`backend/worker.py` 新增 `_PipelineCancelled(BaseException)`（别名 `PipelineCancelled`）、`BidTaskWorker._cancel_flags` 检查点（含 F1 fallback 前置上抛与 ≥5s 节流心跳帧 `"已判定 X / Y 条"`）、`updated_at` SSE 帧透传、清理 `BidFinding`/偏离表及重跑清旧 `stats_json`/`cancelled_by_user`；`backend/routers/checks.py` 新增 `POST /api/checks/{task_id}/cancel`（404/409/审计）与取消期立即 `DELETE`；`backend/routers/radar.py` 将 `POST /api/radar` 与 `/rescan` 接入 `_cancel_flags`（取消 409 `TASK_CANCELLED`、异常落 `failed` 无僵尸 `checking`）；`backend/main.py` 新增 `recover_stale_tasks` 启动恢复钩子；前端 `CheckDetailView.vue`/`CheckTaskList.vue`/`RadarView.vue`/`ExtractView.vue`/`SseProgress.vue`/`http.ts` 补齐中断入口与 120s 判活预警 | 实现 + 验收 |
