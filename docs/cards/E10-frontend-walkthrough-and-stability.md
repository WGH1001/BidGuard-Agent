# 任务卡 E10 · 前端功能走查与并发稳定性卡（列表/删除/大盘统计/CPU 空转）

## 1. 背景与目标

基于全流程压测（并发 2×F2-F4 + 1×F1 + 1×F6 + 法规爬虫）暴露的交互闭环缺口与并发稳定性问题，完成全页面功能走查与后端稳定性加固：
- **设计原则**：凡有产生数据/上传记录的操作，统一配齐「**历史列表 + 点击查看/回放 + 带二次确认（`ElMessageBox.confirm`）的删除**」。
- **基线保护（§0）**：完整保留架构侧已修入基线的 3 处改动（`checks.py` SSE `db.close()` + `database.py` 连接池 20+20、`RulesAndCertsView.vue` `.lc-meta` 换行、`RadarView.vue` `lastAutoTitle` 自动跟随）。

## 2. 交付内容清单（A1 ~ A7）

1. **A1 · F6 控标雷达历史列表 + 查看 + 删除（`backend/routers/radar.py` + `frontend/src/views/RadarView.vue` + `frontend/src/api/index.ts`）**
   - 后端新增 `GET /api/radar`（本租户 `task_type='radar'` 历史列表）、`GET /api/radar/{task_id}`（从 `BidCheckTask` + `BidFinding(source_module='F6')` 还原 `RadarScanOut`）、`DELETE /api/radar/{task_id}`（复用任务级联删除）。
   - 前端 `RadarView.vue` 新增左侧「控标雷达扫描历史」栏，支持点击回放历史评估报告、单条二次确认删除，并与 `sessionStorage` 缓存联动。
2. **A2 · F2-F4 封标防废标体检任务删除增强（`backend/routers/checks.py` + `CheckTaskList.vue` + `CheckDetailView.vue`）**
   - `DELETE /api/checks/{task_id}` 增加运行态拒删守卫：当任务处于 `extracting / checking / grading` 或在 `worker._in_flight` 中时返回 `HTTP 409 TASK_RUNNING`。
   - 终态删除时级联清理 `QaSession`、`BidFinding`、`BidCheckTask` 及磁盘 `data/uploads/{tenant_id}/{task_id}` 上传原件/OCR 伴生缓存与 `output/{deviation,redline,deliver}/{task_id}_*` 导出产物。
   - 前端左侧任务卡与顶部操作栏均提供带 `ElMessageBox.confirm` 的删除按钮，删掉当前选中任务后自动切换下一条或回到空态。
3. **A3 · 大盘台账删除与跳转分流（`frontend/src/views/DashboardView.vue`）**
   - 「在途投标项目防废标体检台账」操作列补齐二次确认删除按钮（删除后立即刷新大盘统计与图表），并根据 `row.task_type` 自动分流跳转至 `/radar` 或 `/checks`。
4. **A4 · E4 法规库管理员级联删除（`backend/routers/regulations.py` + `frontend/src/views/RulesAndCertsView.vue`）**
   - 后端新增 `DELETE /api/regulations/laws/{law_id}`（`Depends(require_admin)`），级联删除该部法规及关联的全部 `RegulationArticle` 条文并记录审计日志。
   - 前端页签 ③ 左栏法规卡片对管理员展示删除按钮，弹窗明确提示「将级联删除《XXX》及该法全部 N 条条文」。
5. **A5 · 大盘 F1 统计口径修复与清零一致性（`backend/worker.py` + `backend/routers/dashboard.py` + `DashboardView.vue`）**
   - `worker.py` 在 `stats_json` 中持久化 `f1_extracted_count`；`dashboard.py` 将租户在途/已交付体检任务的 F1 提取条款数纳入 `defense_matrix` 的 `F1` 统计。全量删除各模块历史任务后，大盘项目总数与六大防线计数一致归零。
6. **A6 · 并发稳定性与空闲 CPU 空转根因治理（`core/bid_aligner.py` + `backend/routers/checks.py` + `frontend/src/api/sse.ts` + `SseProgress.vue` + `frontend/src/api/http.ts`）**
   - **CPU 100% 空转根因修复**：`core/bid_aligner.py` 中 `_seq_ratio` 对 `>2000` 字符长文本启用 8 字符分块 `SequenceMatcher`，`_compute_clause_similarity` 先算 `bigrams` Jaccard 短路低重合长章，`_combine_units` 改用预计算 `bigrams` 并集；实测 7.3 万字招标 PDF × 12.1 万字仿真投标书对齐耗时从 **55.4s（单核 100% 满载）降至 1.05s（提速 50 倍）**。
   - **SSE 断流死循环重连修复**：后端 `stream_check_progress` 监听上限从 20s 提升至 360s（每 15s 发送 `: keep-alive` 帧，任务脱离 `_in_flight` 时短连接核实终态）；前端 `sse.ts` 实现指数退避重连（`1s -> 2s -> 4s -> 8s`，最多 4 次）并在 `SseProgress.vue` 阻断同任务异常态无限重连。
   - **Toast 去重**：`frontend/src/api/http.ts` 增加 3s 滑动窗口相同文案去重，并发请求超时或报错不再连弹多条重复提示。
7. **A7 · 全页面走查补齐（`ExtractView.vue` + `QaChatView.vue` + `ReportsAndAuditView.vue` + `ModelSettingsView.vue`）**
   - `ExtractView.vue` 新增 F1 近期提取历史记录条（支持点击切换查看 + 二次确认删除）；
   - `QaChatView.vue` 会话删除、`RulesAndCertsView.vue` 证照删除、`ModelSettingsView.vue` 解绑模型配置、`ReportsAndAuditView.vue` 交付台账删除全部补齐 `ElMessageBox.confirm` 二次确认。

## 3. 验收实测结果

- 后端全量测试：`128 passed in 31s`（含新增 `test_e10_radar_history_task_delete_guard_regulation_delete_and_dashboard_zero`）
- 前端类型检查与生产构建：`vue-tsc --noEmit && vite build` 零错误通过
