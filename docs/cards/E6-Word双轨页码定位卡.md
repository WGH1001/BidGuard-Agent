# E6 · Word 标书双轨页码定位卡

## 背景
docx 发现的页码此前恒为 `None`（只回退字符偏移 `@字符N`）。用户提出：Word 页面底部印有页码（可能从第 2/3 页才起标、与物理页不一一对应），应**优先用底部印刷页码，没有再用物理页码估算**；且凡估算页码必须提示"不一定准确"。

## 技术方案
OpenXML 里藏着全套分页锚点：`w:lastRenderedPageBreak`（上次保存排版锚点）、`w:br type="page"`（硬分页）、`w:sectPr` 的 `w:pgNumType w:start="N"`（分节页码重置）+ `w:footerReference` 页脚 PAGE 域检测。

## 交付物（已验收，commit `95237ee`）

### 1. `core/parser.py`
- `build_docx_page_spans(file_path)`：解析全文，产出 `[(start, end, 物理页, 印刷页|None)]` span 列表；
- `offset_to_docx_page(offset, spans)`：偏移 → `(物理页, 印刷页|None)`；
- 双轨优先级：节配置印刷页码（decimal 格式才认，其他格式诚实回退）> 物理页估算 > 字符偏移；
- 机器生成 docx（无 Word/WPS 保存的排版锚点）诚实回退空 spans，绝不瞎编。

### 2. `core/deviation_exporter.py` + `core/response_checker.py`
偏离表位置列升级为 `第X页(印刷页Y·估算)`，无锚点回退 `@字符N`；F4 发现页码接入统一映射。

### 3. 前端
`types.ts` 的 `formatPageOrOffset` + `RiskCard.vue` / `DeviationView.vue` 显示"不一定准确"提示（用户要求写死）。

## 验收中的实弹实锤与架构侧修复
真语料探针发现 4 份标书中 1 份（无锡梁溪 5.2 万字）尾部偏移漂移 10 字符——原实现用 lxml 重建全文作偏移空间，文本框/超链接/域字符构造无法逐字节复刻 python-docx 取文逻辑。**修法：偏移权威源换成 python-docx 部件序列（与 parse_document 拼接严格同构，结构上不可能漂移），lxml 退居只供锚点位置**。复测 7/7 份零漂移（连云港印刷页 316 vs 物理 319，分节重置正确）。

## 验收结果
新增 `tests/test_e6_docx_page_marks.py` 4 项，**115 项全绿**；入库 `95237ee`。

## 铁律要点
- **第一验收标准**：新链路重建的 docx 全文与现行 `parse_document` 输出 sha256 逐字节等值（偏移错一位页码映射全废）；
- 凡输出 docx 估算页码处必带"不一定准确"提示；
- 页码格式只认十进制；节上没挂含 PAGE 域的页脚 = 底部没印页码，回退；
- TOC 目录交叉校验明确不做（误校准比不校准危险）。
