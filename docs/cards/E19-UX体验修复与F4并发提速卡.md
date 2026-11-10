# E19 · UX 体验修复与 F4 并发提速卡（六子项 · 强制分 3 个 commit）

> 状态：已完成 · 全量验收通过（Gemini 实现 + Antigravity 交叉复查补修）｜ 立卡：2026-11-10 ｜ 完成：2026-11-10 ｜ 交付 Commit：`85f0ddd` / `f64b3b6` / `b8f3403`（验收线 **160 项全绿** + `vue-tsc` 零错误）
> 来源：用户实测反馈六个问题（F1 时间错 8 小时/白屏/F2-F4 慢/切页面虚假报错/上传体验/F7 布局与思考深度）；Antigravity 全链路排查 + 双方两轮架构把关
> 拆卡纪律：六个子项在 CheckDetailView.vue 上有文件交集，两张卡并行必冲突——合并单卡、强制 3 个 commit（每 commit 独立可回退）；F7 布局改造（左侧抽屉折叠等）不进本卡（待办池）

## 1. 背景（全部代码实锤）

| # | 问题 | 实锤位置 |
|---|---|---|
| 1 | F1 提取历史时间显示凌晨 03:42（实际上午 11 点） | ExtractView.vue L65 `new Date().toISOString()`（UTC，慢 8 小时） |
| 2 | F1 上传用原生 input、右侧框不可点、不支持批量 | ExtractView.vue 上传区；现实多标段场景需排队批量 |
| 3 | F1 大标书提取中途整页白屏卡死感 | ExtractView.vue L277 `v-loading` 挂根 div；且 http.ts L8 全局 `timeout: 180000`（3 分钟）会掐断大标书 LLM 结构化提取 |
| 4 | F2-F4 运行中进度条纹丝不动，无法判断卡死还是核验中 | E11 已落 duration_json（终态），在途态无读秒 |
| 5 | 切页面回来提示失败、切回去还在转 | App.vue L166 `keep-alive` 只含 ExtractView/RadarView/QaChatView——**CheckDetailView 切出即销毁**；SseProgress.vue L106-116 `onError` 无论连接层 Error 还是后端真 failed 帧一律 `localStatus='failed'`（虚假报错，后端 worker 实际还在跑） |
| 6 | F4 连云港 59 万字跑了十几分钟 | response_checker.py L902 for 循环内逐条串行 `client.chat()`（60 条 ×8~10s≈10 分钟） |

**好消息（E19 的 F7 部分网关层零改动）**：`LLMClient.chat()` 已原生支持 `reasoning_effort: Optional[str]=None`（L175），自带两重保护：`supports_reasoning_effort=False` 自动跳过传参（L191）、上游 400/1210 拒绝自动去参重试（L254-256，`_is_thinking_reject`）；`self._lock` 已保护 usage 账本（L160-173）——线程安全已确认。

## 2. 改动清单（三个 commit，严禁混）

### Commit 1 · 纯前端 UX 修复

1. **F1 时区修正**：`ExtractView.vue` 历史时间改浏览器本地时区格式化（`Intl.DateTimeFormat('zh-CN', {...})` 或手拼年月日时分），严禁 toISOString；
2. **F1 上传卡片化 + 批量多文件**：点击/拖拽一体化上传卡片（虚线边框，整块可点可拖入），支持一次多文件排队依次提取；下方近期记录批量陈列（沿用现有 localStorage 历史机制，条数上限可放宽）；
3. **F1 局部 loading + 超时放宽**：取消 L277 整页 `v-loading`，改为上传卡片内进度条 + "正在结构化提取中…"动效，历史列表可正常滚动；`api/index.ts` 的 `extractApi.extract` 单独 `timeout: 600000`（10 分钟），其余 API 沿用全局 3 分钟；
4. **F2-F4 在途读秒**：任务处于 extracting/checking/grading 在途态时，CheckTaskList 列表行与 CheckDetailView 卡片挂实时秒表（`⏱ 已耗时 02:45`，以 started_at 为基准每秒跳动）；任务到达终态后回落显示 E11 的真实总耗时。**UTC 后缀坑（Antigravity 实锤）**：后端 checks.py L128/L324 返回的 started_at 是数据库 naive UTC 的 `.isoformat()`（**不带 Z 后缀**），浏览器会把无 Z 字符串当本地 UTC+8 时间——读秒必须先做规范化补 `'Z'`（复用 CheckDetailView.vue L85 既有 `endsWith('Z') || includes('+')` 模式）再计算 `Date.now() - new Date(normalized).getTime()`，否则任务刚跑 1 秒就会显示 08:00:01；
5. **F7 三挡思考深度**：QaChatView 输入区加 ⚡极速/⚖均衡/🧠深度 三挡切换器，选择持久化到 localStorage；qa 请求体带 `reasoning_effort`。**挡位语义注意**（Antigravity 实锤：llm_gateway L73 在 supports_reasoning_effort=True 时 chat_options 默认已填 'low'，"不传参"实际就是 low，与极速无差异）：**⚡极速='low'、⚖均衡='medium'、🧠深度='high'**，三挡必须显式传值才能真实拉开推理深度差异；未选择时不传参数走网关默认。

### Commit 2 · SSE 状态对齐（消除虚假报错）

1. **`SseProgress.vue` onError 精确分类**：只有收到后端业务帧 `parsed.status === 'failed'` 才 `localStatus='failed'`；`errOrFrame instanceof Error`（连接层中断）**不标 failed**——保持当前状态 + 进入温和的短轮询补偿（每 3~5 秒 `checksApi.detail(taskId)` 拉真实状态，直到后端终态 delivered/failed 才落地展示，轮询期间展示"连接已切换到后台跟踪模式，任务仍在后台运行"类提示）；
2. **CheckDetailView 切回对齐**：onMounted/activated 时主动拉一次 `GET /api/checks/{id}` 对齐真实状态（keep-alive 未命中场景重建时自然触发）；若后端已在途且 SSE 未连，重连 SSE；
3. 严禁改动后端 SSE 帧协议（纯前端修复）。

### Commit 3 · 后端 F4 双阶段并发 + F7 透传

1. **F4 双阶段并发（core/response_checker.py，四条护栏硬约束）**：
   - **阶段 A（按原序串行预定位，严禁进线程池）**：遍历 `valid_reqs` 完成 `_expand_req_text` + `_locate_bid_window_for_req` + 页码解析 + hit_count/direct_gate_hits/hybrid_gate_hits 统计 + 直接漏答处理——**因为并发多线程会同时 np.load/np.save 同一个 `data/cache/{doc_sha}.emb.npy`（hybrid_retriever L310/324/345 无文件锁），轻则重复算 embedding，重则 Windows 下写半截文件 PermissionError**；
   - **阶段 B（仅 `client.chat()` 进线程池）**：`ThreadPoolExecutor(max_workers=int(os.environ.get('BIDGUARD_F4_CONCURRENCY', '4')))`，收集阶段 A 产出的"需调 LLM"任务列表并发执行；单条异常捕获降级（该条标 NEED_MANUAL 类降级状态，不炸整批）；结果**按 req_index 严格升序**归位组装 results 与 findings；末尾 `hybrid_rejected`（约 L1044-1097）跨需求污染降级逻辑保持在并发组装之后原序执行；
   - 护栏：①输出顺序与串行版逐字节一致（同输入断言）；②并发数环境变量配置化默认 4；③`LLMClient._lock` 计量已线程安全（不重复加锁）；④token 用量账本总额不因并发少记；
2. **F7 reasoning_effort 透传**：`bid_react_agent.py` 的 ask 入口（约 L350-421）加默认参数 `reasoning_effort: Optional[str]=None`（零回归），透传到 client.chat；`backend/routers/qa.py`（约 L143-148）请求体/参数接收并透传；非法值（非 low/medium/high）一律忽略走默认。

## 3. 验收标准

1. pytest 新增：① F4 并发结果与串行结果一致性（同输入构造，断言 findings 顺序与内容逐条一致）；② BIDGUARD_F4_CONCURRENCY=1 与 =4 结果一致；③ 单条 chat 抛异常 → 该条降级其余正常；④ reasoning_effort 透传（mock client 断言收到参数；不支持思考的模型断言不传）；⑤ qa 路由非法 effort 值忽略；⑥ F7 三挡断言：极速传 low、均衡传 medium、深度传 high（挡位间必须存在真实参数差异）；
2. 全量 pytest（基线 157 项 + 新增）零回归全绿；`npm run build`（vue-tsc --noEmit && vite build）全链路通过——**vue-tsc 退出码必须在无管道干扰下取**（`cmd > log 2>&1; echo $?`，管道后 $? 是 tail 的）；
3. 前端走查：F1 历史时间为本地时区、拖拽/点击/多文件队列可用、大文件提取时页面可操作；在途任务读秒跳动、终态回落真实耗时；切页面回来不出现虚假"失败"（连接中断态显示后台跟踪提示并自动对齐）；F7 三挡切换生效。

## 4. 铁律

- 三个 commit 严禁混（前端 UX / SSE 对齐 / 后端并发各一个，信息用中文）；
- F4 阶段 A 严禁进线程池（hybrid 缓存 np.load/np.save 无文件锁，并发即损坏）；输出顺序必须与串行版一致；
- SSE 帧协议后端零改动；F7 布局改造（左侧抽屉折叠等）不在本卡范围严禁顺手做；
- 文件白名单：frontend/src/views/ExtractView.vue、QaChatView.vue、CheckDetailView.vue、frontend/src/components/CheckTaskList.vue、SseProgress.vue、frontend/src/api/index.ts、frontend/src/api/types.ts（补 QaAskRequest.reasoning_effort 与 SSEProgressFrame.started_at 等字段）、backend/routers/qa.py、backend/schemas.py（**仅** QaAskRequest 补 reasoning_effort 字段，L255，严禁动其他模型）、core/response_checker.py、core/bid_react_agent.py、可新增测试文件；严禁动 core/llm_gateway.py（网关层零改动）、core/parser.py、core/hybrid_retriever.py。

## 5. 验收与交叉复查记录（2026-11-10）

- **分步提交**：严格按 3 个独立 commit 落盘入库：`85f0ddd`（Commit 1 前端 UX）、`f64b3b6`（Commit 2 SSE 状态对齐）、`b8f3403`（Commit 3 F4 双阶段并发 + F7 透传 + 单测）；
- **Antigravity 交叉复查与实测补修（5 项实锤）**：
  1. `CheckTaskList.vue` 删除错位残留的旧版 `getTerminalDurationLabel` JSDoc 注释（避免误挂 `nowTick` 上方）；
  2. `core/response_checker.py` 删除粘贴替换时遗留的 69 行旧版函数尾部死代码（原 L1272-1340）；
  3. `CheckDetailView.vue` `<SseProgress>` 标签补齐遗漏的 `@progress="handleSseProgress"` 绑定，使实时进度与 `started_at` 正确回流；
  4. `SseProgress.vue` `startBackgroundPolling` 回调加 `targetTaskId` 与 `bgPolling` 守卫，防切换任务时飞行中的旧请求回写污染；统一 `backend/routers/qa.py` / `schemas.py` / 测试文件 CRLF→LF；
  5. `tests/test_e19_ux_and_concurrency.py` 用独立章节分隔 3 条 `★` 要求（修复 `extract_marked_clauses` 把紧邻三行合并为 1 条导致单线程假覆盖的问题），并按 `【招标要求核心句】` 精确分派 Mock，新增 `len(thread_ids) >= 2` 实并发线程数断言；
- **全量实测回执**：
  - `pytest tests/ -q`：**160 passed, 1 warning (231.13s)**（157 基线 + 3 E19 新增）；
  - 无管道 `vue-tsc --noEmit`：**TSC_EXIT=0**；`npm --prefix frontend run build`：**BUILD_EXIT=0**（1727 modules, 8.46s）。
