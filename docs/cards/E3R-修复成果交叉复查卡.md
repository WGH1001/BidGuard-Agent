# E3-R · Antigravity 修复成果交叉复查卡

> 背景：用户真机体验暴露 6 类问题，已由另一实现方（Antigravity）完成一轮修复。
> 你的任务：**交叉复查其修复质量**——验证声称已修的确实修好、排查修复引入的新问题、
> 修复其遗漏的病灶。发现问题直接出修订代码。
> 当前全库 94 项 pytest 全绿、前端 build 零报错（架构侧已机检）。

## 1. 架构侧机检结论（你复查的起点）

| 检查项 | 结果 |
|---|---|
| 94 项既有测试 | ✅ 全绿 |
| 11 份语料 price_cap 命中 | ✅ 11/11 |
| 同文 docx 确定性对齐（run_response_check, client=None） | ✅ 漏应答 0、矛盾 0 |
| 前端 build（TS 5.6 严格检查） | ✅ 零报错 |
| App.vue keep-alive + 各视图 defineOptions 注册 | ✅ 到位 |
| ModelSettingsView autocomplete="off" + text-security 掩码 | ✅ 到位 |
| 链接污染（](http） | ✅ 无 |
| deviation_exporter.py 越卡改动 | ✅ 审查通过（仅 b_start<0 防御 + BOM 清理） |

## 2. 实锤未修好（你的第一优先任务，附复现用例）

**修复方声称"假算术误报已 100% 归零"，但以下用例仍复现**（架构侧刚跑过，未修）：

```python
# docx 表格路径：首数据行"名称=真实品目名，数量/单价/合价 = 1/2/3 连续序号"形态
from docx import Document
from core.price_extractor import extract_price_data
doc = Document()
doc.add_paragraph('开标一览表')
t = doc.add_table(rows=4, cols=5)
for j,h in enumerate(['序号','分项名称','数量','单价（元）','金额（元）']): t.cell(0,j).text=h
for i,r in enumerate([['1','数据库软件','1','2','3'],['2','中间件','','',''],['3','运维服务','','','']], start=1):
    for j,v in enumerate(r): t.cell(i,j).text=v
doc.save(p)
r = extract_price_data(p)
# 实际输出（错误）：items=[{'name':'数据库软件','qty':'1','unit_price':'2.00','total_price':'3.00'}]，status='OK'
# 期望输出：items == []，status == 'NEED_MANUAL'
```

**病灶分析**：修复方只处理了"PDF 文本路径跨行吞序号"和"完全空表回退"两种形态；
**docx 表格路径**（`_extract_from_docx_tables`）里"真实品目名 + 数量/单价/合价恰为 1/2/3 连续序号"
仍被解析成报价行——这是亳州语料空白《分项报价表》的真实形态。修复思路提示：
数量/单价/合价同为 ≤10 的连续小整数（如 1,2,3 / 1,2 / 2,3）且合价≠单价×数量时，
高度疑似行号序列而非报价；也可结合"金额列无千分位/无小数、名称行为空表占位"综合判定。
**修复后此用例必须通过，且不得误伤正常小金额报价行**（如数量=2、单价=3.5、合价=7.00 是合法的）。

## 2b. 补充实锤（v2 新增两项，用户复测确认）

**实锤 A：F1/F6 提取结果 F5 刷新浏览器即丢（修复方未按卡实现）**
- E3 卡原要求"KeepAlive + sessionStorage 镜像"双保险；修复方只做了模块级内存单例，
  **sessionStorage 镜像一个字没写**（架构侧已 grep 核实：ExtractView/RadarView 零命中）。
- 现象：侧栏切换不丢（keep-alive 生效），但浏览器 F5 刷新 = JS 内存全清 = 结果蒸发；
  F2-F4 页面 F5 不丢是因为数据从后端 API 拉取。
- 修法：ExtractView/RadarView 的模块级状态镜像到 sessionStorage（含进行中状态），
  F5 后从 sessionStorage 恢复；**登录用户切换/登出时必须清空**（与 §3.6 安全项同一处理）。

**实锤 B：F2-F4"重新执行体检"后进度条不实时刷新（根因已定位）**
- 病灶：`backend/worker.py` 的 `SSEBroadcaster._latest[task_id]` 存有**上一轮的 done 终态帧**，
  重跑时无任何清理（无 clear 方法，`POST /{id}/run` 也不清）。
- 链路：重跑 → 前端 startStream 重连 → 后端 checks.py 第 333-335 行把 `latest()` 里的
  **旧 done 帧**立即续播给前端 → 前端 onDone 秒触发、状态直接置 delivered/进度 100 →
  进度条冻结，只能手动刷新页面重拉任务 API 才看到真实进度。首次运行无旧帧所以正常。
- 修法：`SSEBroadcaster` 增加 `clear(task_id)`；`POST /{id}/run` 启动新流水线前调用，
  清掉上一轮终态帧；并核查并发场景（清帧与旧 worker 线程竞态——上轮若仍在跑应拒绝重跑，
  该守卫若已存在则只补清帧）。
- 修后验收：同一任务连续两次 run，第二次进度条从 0 实时推进到 100，中途无需手动刷新。

## 3. 复查重点（按风险排序）

1. **backend/worker.py**：修复方给 F2/F3/自定义红线新接了 PDF 页码映射（_build_pdf_page_spans / _locate_f2_anchor）——核查页码映射是否会把 docx 误标页码（必须恒 null）、是否拖慢流水线、异常时是否静默吞错；
2. **core/qualification_scanner.py**：社保月份正则修复（项目编号 czsjcg202610-014 里的 "202610" 不再误判为已覆盖 2026-10）——用构造用例验证：编号含年月后缀时不得误判覆盖，真实社保区间仍正确识别；
3. **core/response_checker.py**：bid_text.find<0 时回退 loc['start'] 的修复、<18 字 PDF 断行碎片扩窗修复——核查两处是否影响 E2 的溯源徽标三分支语义与配对契约（valid_reqs↔star_results 等长）；
4. **core/tender_extractor.py + core/rigging_radar.py**：新增 PDF 页码计算——抽查页码真实性（在原文中可翻到）；
5. **core/price_extractor.py**：修复方将文本路径分隔符改为 [ \t\u3000]——核查是否影响 11 份语料的正常多空格形态；
6. **前端**：ExtractView/RadarView 模块级单例状态缓存——核查跨用户/跨租户登录切换时是否会残留上一账号的数据（**重点安全项**：登录用户切换必须清空模块级缓存）；DeviationView 13 行改动逐行过。

## 4. 设计裁定（不是 bug，不要"修"）

- **同文对齐 + 大模型判"漏应答"**：任务 #12 中两份相同文件有 7 条漏应答，是大模型判定"响应窗口为招标原文照抄、非实质性响应"——这在真实评标尺度下成立（抄题目不等于作答）。是否强制"同文=无偏离"是**产品决策，由用户拍板**，你不改这块逻辑；
- F2/F3 不盖溯源章、docx 无页码显示字符偏移：既定铁律，保持。

## 5. 铁律

1. 94 项既有测试零回归；公共函数签名不删不改；
2. 溯源铁律：展示给用户的"原文"必须原文可搜；页码给不出真值就 null，严禁编造；
3. 不新增 pip 依赖；TS 5.6 严格类型禁 any；
4. 只输出**有改动的文件**完整全文（严禁截断）；无改动的文件列清单说明"已复查，无需改动"及理由；
5. 断言全部数数，严禁空循环假绿。

## 6. 分批输出

- 第一批（约 1500 行）：core/price_extractor.py（§2 实锤修复）+ core/qualification_scanner.py + backend/worker.py（§2b 实锤 B 清帧）。输出完停，等"继续"。
- 第二批（约 1200 行）：core/response_checker.py + core/tender_extractor.py + core/rigging_radar.py + frontend/src/views/ExtractView.vue + frontend/src/views/RadarView.vue（§2b 实锤 A sessionStorage 镜像 + 登出清缓存）+ 新增测试（建议 tests/test_e3_review.py，含 §2 复现用例、§2b 重跑清帧用例与登出清缓存断言）。
