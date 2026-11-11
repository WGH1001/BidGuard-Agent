# E20 · F1/F5/F6 页面体验完善与 Docker 双项目端口共存卡

> 状态：**已验收**（2026-11-11，commit `df9f74a`）｜ 立卡：2026-11-10 ｜ 基线：commit `b468c35`（161 项全绿） → 验收：**166 项全绿** + `vue-tsc` / `vite build` 零错误
> 来源：用户新一轮交互实测反馈（F5 偏离表缺种类筛选 / F6 旧名称文本框误导+批量上传+UTC时区 / F1 历史列表防撑破+5MB LRU 防丢 / 与老项目 Docker 5432 端口冲突无法双开）
> 拆分说明：本卡文件集与 E21（F7 多轮上下文 + 流式思考过程）**完全零交集**，可独立派发与独立验收

## 1. 改动清单（4 个独立模块，建议单卡一并交付、按模块清晰 commit）

### 子项 1 · F5 偏离表预览按种类分类筛选（`frontend/src/views/DeviationView.vue`）

1. **分类筛选栏**：在 `<section class="bg-panel dev-table-card">` 的 `.dt-hd` 下方（预览 `<el-table>` 上方）新增多维状态筛选单选组（`el-radio-group`），并同步支持点击顶部 5 张统计卡片（总数 / 无偏离 / 负偏离 / 漏应答 / 参数冲突）联动切换过滤：
   - `ALL`：`全部 ({{ devResult.rows.length }})`
   - `no_deviation`：`无偏离 ({{ devResult.no_deviation_count }})`（`!row.is_negative`）
   - `negative`：`负偏离（全部）({{ devResult.negative_deviation_count }})`（`Boolean(row.is_negative)`）
   - `missing`：`漏应答 ({{ devResult.missing_count }})`（`row.status === 'MISSING_RESPONSE'`）
   - `contradiction`：`参数冲突 ({{ devResult.contradiction_count }})`（`row.status === 'CONTRADICTION'`）
2. **数据边界守恒**：`filteredRows` 仅作为前端展示过滤数据源，严禁改动后端 F4 三态结果及 `deviationApi.generate` / Word 导出契约（《技术与商务偏离表.docx》仍完整导出全部行）。
*(注：当前 F4 三态中优于要求统一计入 `RESPONSE_OK / 无偏离`，本卡不扩 F4 协议增设 AI“正偏离”状态，以防破坏既定契约与 161 项回归。)*

### 子项 2 · F6 控标雷达去旧文本框、改拖拽批量排队与修时间时区（`frontend/src/views/RadarView.vue`）

1. **移除误导性的 `立项评估任务名称` 输入框**：删除 `customTitle` / `lastAutoTitle` 及其 `<el-input>`，扫完直接用原始文件名（如 `${f.name.replace(/\.[^.]+$/, '')}-投前控标评估`）自动命名——彻底解决“重新进页面总显示上次招标书文件名”的视觉误导；
2. **点击/拖拽一体化多文件批量排队**：参照 `ExtractView.vue` E19 改版，将 `.ru-controls` 的原生 `<input type="file">` 升级为虚线框拖拽上传区（支持一次多选/拖入多个招标文件 `.docx/.pdf/.txt` 排队依次调用 `radarApi.scan` 扫描，展示局部排队进度条，**移除整个右栏 `v-loading="loading"` 全白遮罩锁死**，扫描期间左侧历史列表仍可点击切换）；
3. **修复左栏历史时间差 8 小时**：L244 `(item.created_at || '').slice(0, 16).replace('T', ' ')` 改为 naive-UTC 规范化补 `'Z'` 后用浏览器本地时区 `zh-CN` 格式化，消除凌晨 03:28 偏差。

### 子项 3 · F1 历史列表防撑破页面 + 5MB `localStorage` LRU 淘汰防丢（`frontend/src/views/ExtractView.vue`）

1. **保留顶部视野，优化历史记录区空间**：
   - 不做左侧竖栏（保住下方第 ③ 页签双栏对比与第 ④ 页签 5 列评分细则宽表的充足横向宽度）；
   - 将 `.ext-history-bar` 改为**单行横向可滚动芯片条**（`overflow-x: auto; flex-wrap: nowrap;`），并在栏右侧配备一个带搜索过滤的快速切换下拉框（`<el-select v-model="activeHistoryId" filterable>`），保证即使批量提取 15~20 份标书，历史 bar 依然只有一行高度不把下面清单往下顶；
2. **`localStorage` 5MB 配额超限 LRU 淘汰重试**：
   - 在 `persistHistoryToStorage()` 中：当 `localStorage.setItem(...)` 捕获异常（如多份 59 万字特大标书 JSON 累计超浏览器 ~5MB 配额报 `QuotaExceededError`）时，循环弹出列表尾部最早的旧记录（`trimmed.pop()`）并重试写入，直到成功落盘或仅剩当前最新 1 条，**确保刚提取出的最新成果刷新页面绝不丢失**。

### 子项 4 · Docker 数据库宿主机端口变量化，与老项目双开共存（`docker-compose.yml`、`deploy/.env.docker.example`、`docs/DEPLOY.md`）

1. **`docker-compose.yml` L55**：将 `postgres` 服务端口映射由写死的 `"127.0.0.1:5432:5432"` 改为：
   ```yaml
   ports:
     - "127.0.0.1:${POSTGRES_PORT:-15432}:5432"
   ```
   *(容器内部 `app -> postgres:5432` 走 Docker 虚拟内网，完全不用改；仅改宿主机外露调试端口为 `15432`，从而与老项目 `Contract-Review-Agent` 的 `5432` + `8080` 彻底隔离、随时双开并行运行。)*
2. **`deploy/.env.docker.example`**：新增 `POSTGRES_PORT=15432` 配置项与双项目共存说明；
3. **`docs/DEPLOY.md`**：更新宿主机连容器内 PostgreSQL 跑 pytest 或 DBeaver/Navicat 连库端口说明（默认 `15432`），以及多项目同机并行启动提示。

## 2. 验收标准

1. `npm --prefix frontend run build`（无管道单独查 `vue-tsc --noEmit` 退出码 = 0 且 `vite build` 成功）；
2. `docker-compose.yml` 通过 `yaml.safe_load` 校验无语法错误（含 `POSTGRES_PORT:-15432`）；
3. 全量 `pytest tests/ -q`（基线 161 项）零回归全绿；
4. 前端走查：
   - F5 偏离表支持点击统计卡片或切换按钮瞬时筛选无偏离/负偏离/漏应答/冲突；
   - F6 不再残留旧标书名输入框，支持拖拽多文件批量排队扫描，左侧历史时间与北京本地时间一致；
   - F1 近期历史区保持单行不撑爆，支持下拉搜文件名快速切换。

## 3. 铁律与文件白名单

- 文件白名单：`frontend/src/views/DeviationView.vue`、`frontend/src/views/RadarView.vue`、`frontend/src/views/ExtractView.vue`、`docker-compose.yml`、`deploy/.env.docker.example`、`docs/DEPLOY.md`。
- 严禁碰 F7（`QaChatView.vue`、`QaMessageList.vue`、`ReActSteps.vue`、`core/bid_react_agent.py`、`backend/routers/qa.py`）和后端核心算法引擎（`core/`）。
- 跨平台文件行尾统一 LF（`\n`），严禁 `](http` 等贴入污染。

## 4. 验收实测回执（2026-11-11）

1. **前端构建与类型校验**：`npx vue-tsc --noEmit` 退出码 `0`；`npm --prefix frontend run build`（`vite build`）构建通过。
2. **新增单测（`tests/test_e20_docker_and_ui_contracts.py`）**：`2 passed`（Docker Compose `POSTGRES_PORT:-15432` YAML 契约 + F1/F5/F6 源码契约），同步纳入全量 166 项测试全绿入库。
3. **文件清单**：`frontend/src/views/DeviationView.vue`、`frontend/src/views/RadarView.vue`、`frontend/src/views/ExtractView.vue`、`docker-compose.yml`、`deploy/.env.docker.example`、`docs/DEPLOY.md`、`tests/test_e20_docker_and_ui_contracts.py`（均已统一 LF 换行）。
