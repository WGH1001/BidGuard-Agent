# E38 · F8 写作 Skills 与 AI 动态编排及 E36 异步深层研判卡

> 状态：已实现并全量验收（工作区待确认 commit）｜ 当前代码基线：E36+E37 `caef678`（307 项，13 表 / 13 组路由 / 15 视图）｜ 实测验收：**312 项 pytest 全绿 + `vite build` 1865 模块零错误 + 北交大 04 包 PDF 与 7 份真实语料实弹探针通过**
> 架构边界裁决（2026-11-18 用户定）：
> 1. **一期（E38）不含企业自有 `.docx` 母版上传**：版式与结构完全由 **「Skill 版式约束 + AI 动态控制」** 承担；企业 `.docx` 母版套打独立后置二期（记入 Backlog）。
> 2. **GitHub Skill 导入严禁强依赖外网**：遵循「TUN/代理可用时直连 GitHub；不可用时自动优雅降级尝试 Gitee + 引导本地上传 `.md/.json` 或表单直编」，**严禁下载或执行仓库内任何脚本代码（纯文本规则解析 + SSRF 主机白名单）**。
> 3. **E36 AI 深层研判严守合规边界**：取消 6 份上限仅卡 `>=2` 份；新增可选 AI 语义同构复核与误报排雷综述，严禁输出任何洗稿改写或规避监管建议。

---

## 1. 核心目标

1. **解决 F8 结构与外观写死痛点**：引入 `composer_skills`（写作指令 + 动态版式规范），打通后端 `resolve_effective_llm_client`，由大模型结合选定 Skill 与《招标文件》真实目录/采购需求/评分细则动态规划章节与撰稿；同步修复 E37 `第四部分` 评分细则截断串行 bug 与前端 40 标签砖墙问题，将内部待核对清单与洁净投标正本分离。
2. **打通 Skill 多渠道获取**：支持内置 3 套默认投标 Skill、表单新建/编辑/删改、**GitHub / Gitee 在线预览导入 `SKILL.md`**、以及本地 `.md`/`.json` 文件导入。
3. **解除 E36 数量限制并补强 AI 语义深层对比**：去掉 `<= 6` 份硬限制；增加可选 AI 语义同构识别与通用标准误报排雷综述（过 `verify_quote` 双侧锚定）。
4. **F8 与 E36 切页不中断（异步队列 + `<keep-alive>`）**：两项耗时计算均支持后台线程池异步执行与分阶段进度落库（`queued` / `running` / `completed` / `failed`），前端纳入 [App.vue](file:///e:/agent/BidGuard-Agent/frontend/src/App.vue) `<keep-alive>` 并支持页面重载自动续轮询及服务重启状态恢复。

---

## 2. 技术设计明细

### §2.1 新模块 `core/composer_skills.py`（Skill 规范解析与 GitHub/Gitee 安全导入）
1. **标准 Skill 结构（`WritingSkillSpec` / `SkillLayoutSpec`）**：
   - `writing_instructions`（人设、撰写要求、详略与禁忌）、`tone`（`'formal_gov' | 'technical_deep' | 'concise_direct'`）、`target_section_words`；
   - `layout_spec`：`cover_style`（`'gov_clean' | 'navy_business' | 'tech_modern'`）、`font_zh`、`heading_font_zh`、`font_size_pt`、`line_spacing`、`accent_color_hex`、`table_header_bg`、`include_toc`、`append_checklist_at_end`。
2. **内置 3 套预设 Skill**：`政采严谨全响应版`、`技术架构深度攻坚版`、`简洁高效商务精编版`。
3. **`parse_skill_markdown_or_json(raw_text, source_name='')`**：安全 `yaml.safe_load` 解析 YAML frontmatter + Markdown 正文或 JSON；校验与钳位版式参数，绝不执行脚本。
4. **`import_skill_from_git_url(url_or_repo, *, allow_gitee_fallback=True, timeout=12.0)`**：
   - 支持 `github.com/...`、`raw.githubusercontent.com/...`、`gitee.com/...` 与 `owner/repo` 简写；
   - 严格 SSRF 白名单（仅 `https` 且域名 ∈ `{'github.com','raw.githubusercontent.com','api.github.com','gitee.com'}`），自动透传环境变量代理（TUN）；
   - GitHub 网络超时或不可达时，自动尝试 Gitee 镜像路径；仍失败则抛可识别 `SkillImportError` 引导本地上传或直编，绝不报错崩页。

### §2.2 升级 `core/bid_composer.py`（AI 动态构章 + 分节撰稿 + Skill 驱动排版）
1. **修复遗留误截与重复**：严格过滤 `classify_invalid_clause_role(...) != 'requirement'`（消除 `6、报价过低...评分细则` 串行污染）；偏离表与技术实施方案按职责分离不重复堆砌原文。
2. **AI 动态构章与分节撰稿（`client != None`）**：
   - `_plan_chapters_with_llm`：读取项目元信息 + 章节列表 + 评分细则 + `★/▲` 列表 + Skill 规范，生成定制化技术与商务章节结构；**覆盖率守卫**确保全部 `★/▲` 强制条款与评分主题必有落点，零遗漏；
   - `_draft_chapter_with_llm` / `_draft_deviation_with_llm`：结合招标要求、Skill 指令与 V2 `LocalKnowledgeSnippetProvider` 参考片段并发（`max_workers=4`）撰写正文草稿与结构化辅助表格，并接入 `cancel_check` / `progress_cb`；
   - **三条防废标硬锁**（任何 Skill/AI 均不可突破）：① 报价表金额仅填 `[待人工填报金额...]`；② 证照仅引台账与合法附件、禁止虚构业绩/指标/金额数字；③ 一票否决项全覆盖校验。
   - `client is None` 或网关异常时自动优雅降级，保留 Skill 版式渲染，标注 `ai_status='skipped'`。
3. **动态 Word 版式渲染（`export_composed_bid_docx`）**：
   - 独立居中封面（按 `cover_style` 渲染）+ 分页符 + 原生 Word `Heading 1/2/3`（可选自动目录 TOC 域与章节概览）+ Skill 定制字体/字号/行距/表头底色与 XML 表格列宽/边距；
   - 仅当 Skill `append_checklist_at_end=True` 时在文末附核对附录，保持投标正本干净可读。

### §2.3 升级 `core/bid_similarity.py`（AI 语义同构深层研判）
- 新增 `run_similarity_ai_review(parsed_bids, pair_matrix, findings, client)`：仅在 `enable_ai_review=True` 且 `client != None` 时执行，对高/中风险比对组合的净正文样本做局部窗口研判：
  - 识别近义词洗稿/段落同构（所引两边原句必须双双过 `verify_quote` 核验）；
  - 区分行业通用标准条款误报与高嫌疑同源内容，生成客观核查综述 `ai_review`；
  - 输出强制合规校验（严禁出现洗稿/伪造/规避指导文案）。

### §2.4 数据层、后台异步 Worker 与接口
1. **新增第 14 表 `composer_skills`（`ComposerSkill`）**：含 tenant 隔离、预设标识、指令与版式 JSON。
2. **扩展 `bid_compose_drafts` 与 `similarity_reports`**：均增加 `status`、`progress`、`stage_detail`、`error_msg`、`started_at`、`finished_at`（`bid_compose_drafts` 增 `skill_id`，`similarity_reports` 增 `ai_review_json`），并补齐 `ensure_bid_compose_drafts_columns` / `ensure_similarity_reports_columns` 老库自动加列迁移守卫与 `recover_stale_tasks()` 服务重启复位。
3. **路由升级**：
   - `backend/routers/composer.py`：新增 `/api/composer/skills`（列表/预设自动初始化、新建/修改/删除、`POST /preview-url`、`POST /import-url`、`POST /import-file`）与 `POST /drafts/{id}/cancel`；`/generate` 新增 `skill_id`、`use_ai`、`async_run`（默认 `False` 保既有测试兼容，前端传 `True` 后台异步执行）。
   - `backend/routers/similarity.py`：取消 `<= 6` 上限（仅校验 `>= 2`），新增 `enable_ai_review` 与 `async_run`、`POST /{id}/cancel`；导出报告追加 AI 语义研判综述章节。
4. **前端界面**：
   - [App.vue](file:///e:/agent/BidGuard-Agent/frontend/src/App.vue) `<keep-alive :include>` 新增 `'BidComposerView'` 与 `'SimilarityCheckView'`；
   - [BidComposerView.vue](file:///e:/agent/BidGuard-Agent/frontend/src/views/BidComposerView.vue)：Skill 选择器 + Skill 管理与 GitHub/Gitee/本地上传弹窗、AI 开关、后台任务实时进度条+中断+切页恢复轮询、40 标签砖墙改为紧凑分组待办抽屉；
   - [SimilarityCheckView.vue](file:///e:/agent/BidGuard-Agent/frontend/src/views/SimilarityCheckView.vue)：移除 6 份上限显示、增加 AI 深层复核开关、后台进度条+中断+AI 研判结果展示卡片。

---

## 3. 文件白名单

- `core/composer_skills.py`（新增）、`core/bid_composer.py`、`core/bid_similarity.py`
- `backend/models.py`、`backend/schemas.py`、`backend/worker.py`、`backend/main.py`、`backend/routers/composer.py`、`backend/routers/similarity.py`
- `frontend/src/App.vue`、`frontend/src/api/types.ts`、`frontend/src/api/index.ts`、`frontend/src/views/BidComposerView.vue`、`frontend/src/views/SimilarityCheckView.vue`、`frontend/src/components.d.ts`
- `tests/test_e38_skills_ai_composer_and_async_similarity.py`（新增）、`tests/test_backend_models.py`
- 文档：`INTERFACE.md`、`FRONTEND.md`、`HANDOVER.md`、`AGENTS.md`、`docs/cards/E38-F8写作Skills与AI动态编排及E36异步深层研判卡.md`
