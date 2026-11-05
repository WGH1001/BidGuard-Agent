# FRONTEND.md · BidGuard-Agent 前端契约与视图规格

> 本文档是 `frontend/` 与 `backend/` 之间的唯一接口契约（B2 已验收入库，commit `14d611b`）。
> 前端实现环境读不到 backend 源码，一切请求路径、入参、出参以本文为准；
> 侧栏布局与配色体系以老项目 `Contract-Review-Agent/frontend/src/App.vue` 落地版为蓝本
> （**比根目录 `投标原型图.html` 更成熟**：原型图只作视觉基调参考，严禁 1:1 复刻而牺牲接口真实性）。

---

## 1. 全局约定

- **Base URL**：开发期 Vite 代理 `/api → http://127.0.0.1:8000`（生产同源托管 `frontend/dist`）；
- **鉴权**：除 `/api/auth/login`、`/api/auth/health` 外全部要求 `Authorization: Bearer <token>`；
  令牌为 HMAC 自包含串（非 JWT），有效期 12h，前端存 localStorage 并在 axios 拦截器统一附加；
- **统一错误格式**：非 2xx 一律 `{"detail": {"code": "...", "message": "..."}}`——
  拦截器统一 toast `detail.message`；**401 时清除本地令牌并跳登录页**（含令牌过期）；
  **跨租户资源返回 404 是设计行为**（防探测），提示"不存在或无权访问"即可，不要当 bug 处理；
- **demo 账号**（SEED_DEMO_DATA=1 自动灌入）：`admin / BidGuard2026!`（admin）、`liming / BidGuard2026!`（member）；
- **登录响应**（POST /api/auth/login，入参 `{username, password}`）：
  `{"token": "...", "user": {"id","username","display_name","role","tenant_id"}}`。

## 2. 端点总清单（10 组，路径即真实路径）

| 视图消费方 | 端点 | 说明 |
|---|---|---|
| 登录 | `GET /api/auth/health`、`POST /api/auth/login`、`GET /api/auth/me`、`POST /api/auth/change_password`（入参 `{old_password,new_password}`） | me 返回 `{id,username,display_name,role,tenant_id,is_active,last_login_at}` |
| Dashboard | `GET /api/dashboard` | 返回 `DashboardStatsOut`，含 `total_projects`、`defense_matrix`（**6 元素数组**，对应封标前六大一票否决防线健康度）、近期任务与截标倒计时数据 |
| F1 提取 | `POST /api/extract`（multipart `file`，docx/pdf/txt） | 返回四张清单：`marked_clauses`（含 `kind` 字段：requirement/usage_note/table_marker）、`invalid_bid_clauses`、`scoring_tables`、`scoring_prose` + `llm_status`（'done'/'skipped'）；**usage_note/table_marker 要原样展示但打"非要求"标签**供人工核对 |
| 体检任务 | `POST /api/checks`（multipart：`tender_file`+`bid_file` 必传，`title`/`open_date`/`buyer_name`/`auto_run` 可选 Form） | 返回 `BidTaskOut`，含 `tender_url`/`bid_url`（下载链接） |
| | `GET /api/checks?status=&task_type=&page=&page_size=` | 分页 `{total, items}` |
| | `GET /api/checks/{id}` | `{task: BidTaskOut, findings: BidFindingOut[]}` |
| | `POST /api/checks/{id}/run?sync=false` | 触发流水线；前端一律用异步 + SSE |
| | `GET /api/checks/{id}/stream` | **SSE**，见 §3 |
| | `GET /api/checks/{id}/findings?source_module=&severity=` | `{total, items}` |
| | `POST /api/checks/{id}/findings/{fid}/feedback`（`{feedback:'accepted'/'rejected', feedback_note}`） | 采纳/驳回回写 |
| | `POST /api/checks/{id}/price-manual`（`{items:[{name,qty,unit_price,total_price}], table_total, bid_total, bid_total_upper, price_cap}`，金额全字符串） | NEED_MANUAL 回填：后端 PriceSandbox 复算并**替换全部 F2 findings**，返回 `findings`（有算术错时非空） |
| | `DELETE /api/checks/{id}` | — |
| 偏离表 | `POST /api/deviation/{task_id}`（重复调用重新生成） | 返回 `{output_filename, download_url, total_rows, no_deviation_count, negative_deviation_count, missing_count, contradiction_count}` |
| | `GET /api/deviation/{task_id}/download` | docx 文件流 |
| 控标雷达 | `POST /api/radar`（multipart `tender_file`） | 返回 `{suspicious_params:[{quote,offset,exclusive_type,confidence,reason,quote_verified,llm_status}], conclusion:{recommendation,summary}}`；recommendation ∈ 值得投/谨慎投/疑似控标高，**UI 必须标"疑似"、不替用户拍板** |
| F7 问答 | `POST /api/qa/sessions`（`{task_id?}`）、`GET /api/qa/sessions`、`GET/DELETE /api/qa/sessions/{id}` | 会话含 `messages_json: [{role:'user'/'assistant', content, quotes, steps}]` |
| | `POST /api/qa/sessions/{id}/ask`（`{question}`，非流式，等待可达 1-2 分钟） | 返回 `{mode:'react'/'keyword_fallback', answer, quotes:[{source,quote,offset,page,quote_verified}], steps, rounds_used}`；**page 仅 PDF 有值，docx 为 null，UI 显示"字符偏移 X"** |
| 规则与证照 | `GET/POST /api/rules`、`PUT /api/rules`（规则配置，字段含 `price_cap`(字符串金额)/`open_date`/`buyer_name`/`required_social_months`/`custom_redlines:[{keyword,severity,consequence}]`） | 每租户多条，`is_default` 标默认 |
| | `GET/POST /api/rules/certificates`、`PUT/DELETE /api/rules/certificates/{cert_id}` | 证照台账 `{cert_name,cert_no,valid_until,warn_days,remark}`；前端按 `valid_until - today <= warn_days` 做到期预警高亮 |
| 交付与审计 | `GET /api/reports/{task_id}/redline` | 标红批注原件（docx/pdf 文件流） |
| | `GET /api/reports/{task_id}/deliver.zip` | 交付包 ZIP（标红原件+偏离表+签字单） |
| | `GET /api/reports/audit-logs`（admin，403 拦 member） | `{total, items:[{action,target,ip,created_at,...}]}` |
| 模型配置 | `GET/PUT /api/model-config`、`DELETE /api/model-config`、`POST /api/model-config/test` | PUT 入参 `{base_url,api_key,model_flash,model_flagship,supports_reasoning_effort}`；**响应永不回显明文 Key**，只回 `{configured:true, api_key_last4:"abcd", ...}`；test 为连通性测试 |

文件下载统一走 `GET /api/files/{相对路径}`（已做租户归属校验，跨租户 404）。

## 3. SSE 进度流协议（CheckDetailView 消费）

`GET /api/checks/{task_id}/stream`（EventSource 需自带 token → 用 fetch 流式读取实现，EventSource 不支持自定义 header，蓝本 `api/sse.ts` 已有方案）：

```text
event: progress   data: {"task_id":1,"status":"extracting|checking|grading","progress":55,"stage_detail":"..."}
event: done       data: {"task_id":1,"status":"delivered","stats":{"total_findings","error_count","warning_count","price_status","hit_rate","llm_status",...}}
event: error      data: {"task_id":1,"status":"failed","error_msg":"..."}
```

- 状态机：`created → extracting → checking → grading → delivered`（异常 failed）；
- **断线重连后重订阅会先收到终态/当前进度帧**，UI 按 status 幂等处理；
- `stats.price_status == 'NEED_MANUAL'` 时弹出报价人工回填弹窗（调 price-manual 接口）。

## 4. 视图 ↔ 端点映射（8 业务视图 + 3 辅助视图）

1. `DashboardView`：/api/dashboard（ECharts：4 统计卡 + 六大防线矩阵热力/仪表 + 截标倒计时列表 + 在途任务台账）；
2. `ExtractView`：/api/extract 上传 → 四张清单四 Tab 核对（kind 标签区分）；
3. `CheckDetailView`（最重）：/api/checks 全组 + SSE——6 步流水线进度条、风险卡列表（按 severity 分级配色，采纳/驳回按钮）、报价回填弹窗、投标书原件下载与标红预览入口；
4. `DeviationView`：/api/deviation 预览统计 + 一键导出下载；
5. `RadarView`：/api/radar 上传 → 疑似参数卡片流 + 结论横幅（"疑似"标注）；
6. `QaChatView`：/api/qa 全组——会话侧栏（KeepAlive 切页防丢）+ 消息流（ReAct steps 折叠面板 + quotes 溯源卡）；
7. `RulesAndCertsView`：/api/rules + certificates 两页签 CRUD；
8. `ReportsAndAuditView`：交付中心（按任务列 redline/deliver.zip 下载）+ 审计日志表（admin）；
9. 辅助：`LoginView` / `ChangePasswordView` / `ModelSettingsView`（/api/model-config + test 连通性按钮）。

## 5. 工程约定

- Vue 3 `<script setup lang="ts">` + Vite + TS + Element Plus（unplugin 自动按需引入）+ ECharts（按需注册）；
- 侧栏三主题色一键切换（默认工业浅岩灰 `#e2e8f0`、深暖炭褐灰、莫兰迪深青灰），切换状态存 localStorage；
- **docx 页码一律可能为 null**（仅 PDF 有真实页码），所有展示页码的位置必须兼容 null → 显示"字符偏移 @N"；
- 金额展示两位小数、大写金额原样展示；severity 配色：error 红 / warning 橙 / info 灰蓝；
- API 封装分模块（api/index.ts 汇出），类型集中 `api/types.ts`（与 §2 出参逐字段对齐）。

## 6. 分卡计划

| 卡 | 范围 |
|---|------|
| C1 前端基座 | 脚手架 + App 布局三色侧栏 + 路由守卫 + api/http+sse 封装 + Login/改密/ModelSettings + DashboardView |
| C2 业务流一 | ExtractView + CheckDetailView + DeviationView + RadarView |
| C3 业务流二+收尾 | QaChatView + RulesAndCertsView + ReportsAndAuditView + build 联调 |

每卡验收：本地 `npm run build` 零报错 + 关键页面冒烟（架构侧执行）。
