# 任务卡 B2 · backend 路由层（schemas + 10 组路由 + main 装配 + SSE 流水线）

> 执行者零上下文进场：先读根目录 `INTERFACE.md`（全文），再读本卡，然后动手。
> 前置：B1 卡（数据层）已完成入库——`backend/database.py` / `models.py` / 完整版 `auth.py` 可直接使用。
> 蓝本项目：`E:\agent\Contract-Review-Agent\backend\`（`schemas.py`、`routers/`、`main.py`、`worker.py`、`cache.py`、`pathing.py`）。

---

## 1. 任务背景

数据层（B1）已就绪。本卡把 core 引擎层 F1~F7 通过 10 组 API 路由暴露给前端，含上传、
后台核验流水线、SSE 进度流、偏离表导出与交付打包。**本卡完成后 backend 即功能完整**，
前端（下一阶段）只对本卡暴露的接口编程。

## 2. 交付物

| 文件 | 动作 | 要求 |
|------|------|------|
| `backend/schemas.py` | 新建 | Pydantic v2 请求/响应契约；所有路由响应显式 response_model，禁裸 dict 出路由 |
| `backend/routers/` 下 10 个文件 | 新建 | 见 §3 路由清单 |
| `backend/main.py` | 重写（当前是空壳） | FastAPI 装配：CORS（读 CORS_ORIGINS）、挂载 10 组路由、启动时 `create_all` + `SEED_DEMO_DATA=1` 时灌演示租户/用户/规则/证照、静态托管 `frontend/dist`（存在才挂）、全局异常 → `INTERFACE.md §3.5` 统一错误格式 |
| `backend/worker.py` | 新建 | 进程内线程池任务执行器：按 `INTERFACE.md §3.3` 状态机推进 bid_check_tasks，逐阶段写回 progress/stage_detail，产出 findings 落 `bid_findings`；REDIS_URL 未配置时纯进程内模式（Redis 队列留接口不强做） |
| `tests/test_backend_routers.py` | 新建 | 见 §5 验收标准 |

## 3. 路由清单（10 组，路径与职责不得更名）

1. **`/api/auth`**：login（签发 HMAC 令牌）、me、change_password、health。
2. **`/api/dashboard`**：总览统计（4 统计卡 + 近期任务 + 截标倒计时数据），全部按 tenant 过滤聚合。
3. **`/api/checks`**：任务 CRUD + 双文件上传（multipart，落 `data/uploads/{tenant}/{task}/`，白名单 docx/pdf/txt，路径穿越校验见 `INTERFACE.md §3.4`）+ `POST /{id}/run` 触发流水线 + `GET /{id}/stream` SSE + findings 反馈（accepted/rejected）+ NEED_MANUAL 报价人工回填 + `GET /{id}/findings`。
4. **`/api/extract`**：上传招标文件 → `extract_tender_with_llm` + `extract_scoring_with_llm`，四张清单结构化返回（usage_note/table_marker 原样带出供前端核对，但标注 kind）。
5. **`/api/deviation`**：基于已有 task 调 `export_deviation_table`，产物落 `output/deviation/`，返回下载地址与统计行数。
6. **`/api/radar`**：上传招标文件 → `scan_rigging_radar`，落库为 task_type=radar 的任务与 findings。
7. **`/api/qa`**：会话 CRUD + `POST /{session_id}/ask` 调 `ask_bid_expert`，整轮 Q&A（含 quotes/steps）追加进 `qa_sessions.messages_json`；docx 页码为 null 时响应里保留 null。
8. **`/api/rules`**：bid_rule_configs CRUD + company_certificates CRUD（证照到期预警列表：`valid_until - today <= warn_days`）。
9. **`/api/reports`**：交付中心——单任务打包（标红原件 + 偏离表 + 终核签字单）ZIP 下载；审计日志查询（admin only）。
10. **`/api/model-config`**：user_model_configs 读写，API Key 写入后响应永不回显明文（只回 `configured: true` + 尾四位）；生效时经 `build_client_from_user_config` 构造客户端。

## 4. 实现铁律（违反即打回）

1. **只走契约入口**：调用 core 一律用 `INTERFACE.md §1` 登记的函数与字段名；禁止 import `_` 私有函数；F4 需要要求清单时把 F1 结果透传（`requirements=` 参数），不要让它内部重复解析。
2. **多租户隔离**：所有查询强制 tenant 过滤；跨租户资源返回 404。写操作落 audit_logs。
3. **降级优先**：LLM client 按"用户自定义配置 → 全局 env 配置 → None（0 Token 确定性降级）"三级获取；任何一层失败不得 500，必须走降级并在响应标注 llm_status。
4. **SSE 纪律**：按 `INTERFACE.md §3.3` 三种事件；任务在后台线程跑，客户端断连不中断；docx 页码输出 null 不编造。
5. **F1 降级特例**：`extract_tender_with_llm` 的 `client=None` 是"自动加载全局网关"而非 0 Token 降级（未配 Key 会抛 RuntimeError，见 `INTERFACE.md §1` 开头分列声明）——/api/extract 与流水线在无模型降级模式下必须**显式直调 `build_review_checklist()`** 确定性层，严禁触发 tender_llm 的隐式联网加载。
6. **redline 回写适配**：`inject_word_comments` / `inject_pdf_highlights` 按对象属性访问 finding（`getattr(finding,'quote')` 等，详见 `INTERFACE.md §1.8`）。BidFinding ORM 已内置 `.rule_name` / `.legal_basis` 只读属性可直接用；但 **severity 枚举必须先映射**（BidFinding 的 error/warning/info → redline 认的 high/medium/low），逐条包装成含全部所需属性的轻量对象再传入。**严禁裸传 dict**（不会报错而是静默取空、全部退化为"全篇/低风险"）。
7. **测试 mock docx 构造纪律**（B1 已验证的既有模式，照抄）：`Document()` 逐段 `add_paragraph` 后 `tempfile.mkstemp(suffix='.docx')` 保存；**★行之间必须插非标记中性行**（如"上述为运行环境基线要求。"），否则连续 ★ 行会被解析层合并成一条记录；投标书段落须含与招标 ★ 条款同文的关键句以构造响应命中。
8. 金额 Decimal：price_cap 等金额用 `Numeric`/`Decimal` 传递，路由层禁 float 运算。
9. **不动别人的东西**：不修改 core/ 任何文件、不修改 backend/models.py / database.py / auth.py / utils/crypto.py（B1 产物，只读复用；发现契约冲突停下来报错，不要自行改契约）；不建 frontend/。
10. 库模块静默、`encoding='utf-8'`、PowerShell 5.1 命令逐行执行。
11. **core 调用只认契约**：执行环境读不到 core/ 源码，一切入口签名与返回结构以 `INTERFACE.md §1` 为准，严禁凭想象补参数或字段。

## 5. 验收标准（一卡一验，全绿入库）

1. `tests/test_backend_routers.py` 用 FastAPI TestClient + sqlite 临时库 + `client=None`（0 Token，不烧钱）覆盖至少：
   - 登录→拿令牌→访问 me；无令牌 401；member 访问 admin 接口 403；
   - 上传→建任务→run→轮询/SSE 到 delivered（F1~F4 走确定性层真实执行，用 tests 里既有的 mock docx 构造方式，**★行间必须插非标记中性行**防解析合并）；
   - findings 落库与反馈回写；NEED_MANUAL 回填接口；
   - 租户隔离：B 租户访问 A 租户任务 404；
   - deviation 导出产物文件存在且 total_rows>0；qa ask 走 keyword_fallback 模式返回 quotes；
   - **所有断言数数，严禁空循环假绿**。
2. 全量 `pytest tests/ -q` 零回归（基线 ≥ B1 完成后总数）。
3. commit message 首行：`feat(B2): backend routers - 10 API groups + SSE pipeline + main assembly`。

## 6. 本卡涉及文件清单（实现前先逐个读取原文，执行环境以随卡附件为准）

```text
INTERFACE.md                              # 契约全文（必读，§1 签名/§2 表/§3 横切约定）
backend/main.py                           # 重写（当前 2 行空壳）
backend/database.py / models.py / auth.py # B1 产物，只读复用（禁止改签名）
backend/utils/crypto.py                   # 只读复用
蓝本_schemas.py / 蓝本_main.py / 蓝本_worker.py            # 老项目蓝本（只读参考）
蓝本路由_auth / dashboard / tasks / qa / qa_sessions /    # 老项目蓝本（只读参考，
rules / reports / model_config / audit）                    # BidGuard 无 knowledge/diff，已剔除）
core/ 各模块                              # 读不到源码，一律按 INTERFACE.md §1 契约调用，禁止修改
```
