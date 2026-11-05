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
# 返回：{'findings': [...],            # MISSING_RESPONSE + CONTRADICTION 汇总
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
```

### 1.8 批注回写与解析底座

```python
# core/parser.py
parse_document(file_path: str) -> ParsedDoc          # .text / .source_type / .pages

# core/redline.py —— 原位标红批注（交付中心 ZIP 用）
inject_word_comments(src_docx_path: str, findings: list, ...)   # 签名详见源码 docstring
inject_pdf_highlights(src_pdf_path: str, findings: list, ...)
```

### 1.9 模型网关（含用户级自定义 Key）

```python
# core/llm_gateway.py
get_llm_client(env: Optional[str] = None) -> LLMClient      # 读 config/models.yaml
build_client_from_user_config(base_url: str, api_key: str, model_flash: str,
                              model_flagship: Optional[str] = None,
                              supports_reasoning_effort: bool = False) -> LLMClient
# user_model_configs 表解密出的明文 key 只传参驻留内存，严禁写日志/落盘/进响应
```

---

## 2. 数据库 8 组表规格（backend/models.py）

> SQLAlchemy 2.0 风格；PostgreSQL 16 主库，`DATABASE_URL` 为 sqlite 时无缝回退（开发/单测）。
> 所有表含 `id`（自增主键）、`created_at`；更新型表另含 `updated_at`。时间一律 UTC。

| # | 表名 | 职责 | 关键字段（除公共外） |
|---|------|------|----------------------|
| 1 | `tenants` | 多租户投标部门 | `name`（唯一）、`is_active` |
| 2 | `users` | 用户与角色鉴权 | `tenant_id` FK、`username`（**全局唯一** unique=True，登录入参不带租户标识，租户内唯一无法定位用户）、`password_hash`(bcrypt)、`display_name`、`role`(admin/member)、`is_active`、`last_login_at` |
| 3 | `bid_check_tasks` | 体检/控标任务主表 | `tenant_id`、`user_id`、`task_type`(pre_check/radar)、`title`、`status`(created→extracting→checking→grading→delivered，含 failed/need_manual)、`progress`(0-100)、`stage_detail`、`tender_filename`、`bid_filename`、`tender_path`、`bid_path`、`error_msg`、`stats_json`、`price_manual_json`(NEED_MANUAL 回填)、`config_json`(开标日/限价/招标人/社保月份快照)、`llm_usage_json`、`delivered_at` |
| 4 | `bid_findings` | 检出风险明细 | `task_id` FK(级联删除)、`source_module`(F1/F2/F3/F4/F6)、`code`、`severity`(error/warning/info，映射 high/medium/low 由路由层定)、`category`、`clause_no`、`quote`(主摘录：F4 存招标要求侧原文，其余模块存命中原文)、`bid_quote`(可空，F4 双书勾稽时存投标书对应摘录)、`quote_offset`、`quote_verified`、`page`、`message`、`suggestion`、`consequence`、`detail_json`(模块特有字段兜底：F4 双书偏移 / F2 差额明细 / F6 的 exclusive_type、confidence / F7 的 quotes 与 steps)、`feedback`(pending/accepted/rejected)、`feedback_note` |
| 5 | `bid_rule_configs` | 核查参数与自定义红线 | `tenant_id`、`name`、`price_cap`(Numeric 14,2)、`open_date`(Date)、`buyer_name`、`required_social_months_json`、`custom_redlines_json`([{'keyword','severity','consequence'}])、`is_default` |
| 6 | `company_certificates` | 企业证照效期台账 | `tenant_id`、`cert_name`、`cert_no`、`valid_until`(Date)、`warn_days`(默认 30)、`remark` |
| 7 | `qa_sessions` | F7 追问会话持久化 | `tenant_id`、`user_id`、`task_id`(可空 FK)、`title`、`tender_path`、`bid_path`、`messages_json`([{'role','content','quotes','steps'}]) |
| 8 | `audit_logs` | 安全审计留痕 | `tenant_id`、`user_id`、`action`、`target`、`detail_json`、`ip` |
| 9 | `user_model_configs` | 用户级自定义模型配置 | `user_id`(唯一 FK)、`base_url`、`api_key_encrypted`、`model_flash`、`model_flagship`、`supports_reasoning_effort`(bool) |

金额字段一律 `Numeric(14, 2)`（对应 Decimal，禁 float）；JSON 字段后缀 `_json`，
SQLite 回退用 `sa.JSON`，PG 下可无差别使用。

---

## 3. backend 层横切约定

### 3.1 目录与模块

```text
backend/
├── main.py        # FastAPI 装配：CORS、路由挂载、启动建表 + SEED_DEMO_DATA、静态托管 frontend/dist
├── database.py    # engine/SessionLocal/Base/get_db；DATABASE_URL 解析（sqlite 相对路径锚定项目根）
├── models.py      # §2 的 9 张表 ORM
├── schemas.py     # Pydantic 请求/响应契约（响应模型一律显式声明，禁裸 dict 出路由）
├── auth.py        # HMAC-SHA256 令牌签发/校验、bcrypt 校验、get_current_user / require_admin 依赖、审计
├── utils/crypto.py# 用户 API Key 派生加密（已就绪，主密钥 = get_auth_secret()）
└── routers/       # 10 组路由：auth dashboard checks extract deviation radar qa rules reports model_config
```

蓝本：`E:\agent\Contract-Review-Agent\backend\`（auth.py / database.py / models.py / worker.py 等
继承其成熟逻辑，字段按 §2 裁剪，业务语义按本项目改写）。

### 3.2 鉴权与多租户

- 登录 `POST /api/auth/login` → 返回 HMAC 令牌（**payload 权威键名锁死为 `sub` / `tenant_id` / `role` / `exp`，沿用蓝本项目规范；禁止再塞任何别名键**）；
- 除 `/api/auth/login`、`/api/auth/health` 外全部路由挂 `get_current_user` 依赖；
- **一切数据读写必须带 `tenant_id` 过滤**（任务/发现/规则/证照/会话/审计），跨租户访问返回 404 而非 403（防探测）；
- 写操作（POST/PUT/DELETE）经 `audit_logs` 留痕。

### 3.3 SSE 进度流协议（`GET /api/checks/{task_id}/stream`）

```text
event: progress   data: {"task_id":1,"status":"checking","progress":45,"stage_detail":"★条款核验 12/38"}
event: done       data: {"task_id":1,"status":"delivered","stats":{...}}
event: error      data: {"task_id":1,"status":"failed","error_msg":"..."}
```

- 后台执行用进程内线程池（Redis 队列为可选增强，`REDIS_URL` 未配置时纯进程内双模）；
- 流水线状态机严格按 §2 表 3 的 `status` 五态推进；
- 客户端断连不得中断任务，重连后从当前 status 续播。

### 3.4 文件存储布局

```text
data/uploads/{tenant_id}/{task_id}/tender.{docx|pdf}   # 原样保存，磁盘文件名用固定名防路径穿越
data/uploads/{tenant_id}/{task_id}/bid.{docx|pdf}
output/deviation/{task_id}_偏离表.docx                  # F5 产物
output/redline/{task_id}_标红原件.{docx|pdf}            # redline 回写产物
output/deliver/{task_id}_交付包.zip                     # 交付中心打包
```

上传校验：扩展名白名单（docx/pdf/txt）+ 大小上限（默认 100MB）+ `Path.resolve()` 后必须落在
`data/uploads/` 根内。**数据库只存相对路径，序列化给前端时拼 `/api/files/...` 下载路由。**

### 3.5 统一错误格式与环境变量

```json
{"detail": {"code": "TASK_NOT_FOUND", "message": "任务不存在或无权访问"}}
```

`.env.example` 14 变量以 `HANDOVER.md §8.2` 清单为准（B1 卡负责落地扩充）。
`DEBUG` 未显式置 1 且 `AUTH_SECRET` 缺失时拒绝启动（auth.py 已有守卫，B1 复用不重造）。

---

## 4. 变更登记（追加，不删除）

| 日期 | 变更 | 登记人 |
|------|------|--------|
| 2026-11-05 | 初版：锁死 core 九类入口签名 + 9 表规格 + 横切约定 | 架构侧 |
| 2026-11-05 | B1 卡审前修订（实现方 Gemini 提出 5 条，采纳 3 修正 1 驳回 1）：① §1 降级声明按模块分列（F1 tender_llm 的 client=None 是自动加载网关，非 0 Token 降级）② users.username 改全局唯一 ③ bid_findings 增 detail_json 兜底列 ④ 令牌 payload 键名锁死 sub/tenant_id/role/exp | 架构侧 |
| 2026-11-05 | B1 验收修正（实现方未收到审前修订，两处按原建议实现，验收时纠正）：① users.username 落实全局唯一（替换 UniqueConstraint(tenant_id,username)）② create_token payload 删除 uid/user_id/tenant 别名键，仅留 sub/tenant_id/role/exp，get_current_user 同步只读权威键。另：实现方加的 bid_quote 列经验收裁定保留并补登 §2 表 4；BidFinding 增加 rule_name/legal_basis 只读属性（B2 调 redline 兼容用，验收认可） | 架构侧 |
