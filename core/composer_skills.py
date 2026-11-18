# -*- coding: utf-8 -*-
"""E38 · F8 标书写作 Skills 规范解析与 GitHub/Gitee 安全导入器。

安全与离线原则：
1. 纯文本声明式规则解析（YAML frontmatter Markdown / JSON），严禁下载或执行仓库内任何脚本；
2. 仅允许 https 访问 ALLOWED_GIT_HOSTS（github.com / raw.githubusercontent.com / api.github.com / gitee.com），防 SSRF；
3. 自动尊重环境变量代理（TUN / HTTP_PROXY / HTTPS_PROXY）；
4. GitHub 网络不可达或超时时，若 allow_gitee_fallback=True 自动尝试 Gitee 同路径；
   两者皆不可达时返回明确可操作的 SkillImportError（引导本地上传 .md/.json 或直接表单编辑），
   严禁把外网可达性作为功能前置依赖。
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Optional
from urllib.parse import urlparse

import requests
import yaml

from core.indicator_library import normalize_skill_biz_line

ALLOWED_GIT_HOSTS = frozenset({
    'github.com',
    'raw.githubusercontent.com',
    'api.github.com',
    'gitee.com',
})

_VALID_COVER_STYLES = frozenset({'gov_clean', 'navy_business', 'tech_modern'})
_VALID_TONES = frozenset({'formal_gov', 'technical_deep', 'concise_direct'})
_VALID_FONTS = frozenset({'宋体', '仿宋', '黑体', '微软雅黑', '楷体'})
_HEX_COLOR_RE = re.compile(r'^[0-9A-Fa-f]{6}$')
_SHORTHAND_REPO_RE = re.compile(r'^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/(.+))?$')

DEFAULT_LAYOUT_SPEC: dict[str, Any] = {
    'cover_style': 'gov_clean',
    'font_zh': '仿宋',
    'heading_font_zh': '黑体',
    'font_size_pt': 11.5,
    'line_spacing': 1.35,
    'accent_color_hex': '0F172A',
    'table_header_bg': 'F1F5F9',
    'include_toc': True,
    'append_checklist_at_end': False,
    'target_section_words': 500,
    'tone': 'formal_gov',
}

BUILTIN_WRITING_SKILLS: tuple[dict[str, Any], ...] = (
    {
        'name': '政采严谨全响应版',
        'description': '公文体严谨应答风：仿宋正文字体、标准封面与目录，逐条点对点响应招标要求，零空话、零遗漏。',
        'biz_line': 'general',
        'source_type': 'builtin',
        'is_builtin': True,
        'is_default': True,
        'writing_instructions': (
            '1. 严格站在响应方视角使用正式书面公文用语，按招标采购需求与评分细则逐条拆解响应；\n'
            '2. 每节先给出明确「完全响应结论」，再展开具体技术实现路径、参数匹配依据、实施排期与验收标准；\n'
            '3. 对系统交付周期、质保响应时效、培训与售后人员职责给出可核查的条理化清单；\n'
            '4. 严禁虚构招标文件及企业台账中未出现的合同金额、过往业绩年份与虚假认证编号，遇未定指标明确保留待人工确认占位。'
        ),
        'layout_spec': {
            'cover_style': 'gov_clean',
            'font_zh': '仿宋',
            'heading_font_zh': '黑体',
            'font_size_pt': 12.0,
            'line_spacing': 1.45,
            'accent_color_hex': '0F172A',
            'table_header_bg': 'F1F5F9',
            'include_toc': True,
            'append_checklist_at_end': False,
            'target_section_words': 550,
            'tone': 'formal_gov',
        },
    },
    {
        'name': '技术架构深度攻坚版',
        'description': '重工程架构细节与高分打法：微软雅黑现代商务排版、深蓝高亮表头，突出高可用指标、安全机制与实施表格。',
        'biz_line': 'general',
        'source_type': 'builtin',
        'is_builtin': True,
        'is_default': False,
        'writing_instructions': (
            '1. 重点围绕评分高分点深挖技术实现机理：清晰阐明系统总体架构、核心模块交互、国产化适配及安全容灾方案；\n'
            '2. 善用结构化对比表格（如职责分工表、里程碑排期表、SLA 响应承诺表）替代大段流水账堆砌；\n'
            '3. 针对关键运维保障、应急演练与数据安全防泄密场景给出分步骤处置预案；\n'
            '4. 保持数据诚实：不捏造性能跑分、项目合同金额或外部品牌参数，需补测指标用清晰方括号标注。'
        ),
        'layout_spec': {
            'cover_style': 'tech_modern',
            'font_zh': '微软雅黑',
            'heading_font_zh': '微软雅黑',
            'font_size_pt': 10.5,
            'line_spacing': 1.35,
            'accent_color_hex': '1E40AF',
            'table_header_bg': 'DBEAFE',
            'include_toc': True,
            'append_checklist_at_end': False,
            'target_section_words': 700,
            'tone': 'technical_deep',
        },
    },
    {
        'name': '简洁高效商务精编版',
        'description': '紧凑高信噪比评审友好版：宋体清晰排版、青黛色表头，直击核心得分要素，适合快速审阅与二次精修。',
        'biz_line': 'general',
        'source_type': 'builtin',
        'is_builtin': True,
        'is_default': False,
        'writing_instructions': (
            '1. 行文干练紧凑、重点突出，每小节聚焦「核心响应点 + 关键措施 + 保障承诺」三段式表达；\n'
            '2. 尽量避免冗余客套铺垫，优先用编号要点和紧凑对照表提升评标专家检索得分点的效率；\n'
            '3. 报价、法定代表人签章及台账未收齐的证照严格保留醒目待人工填报标识。'
        ),
        'layout_spec': {
            'cover_style': 'navy_business',
            'font_zh': '宋体',
            'heading_font_zh': '黑体',
            'font_size_pt': 10.5,
            'line_spacing': 1.25,
            'accent_color_hex': '0F766E',
            'table_header_bg': 'ECFDF5',
            'include_toc': True,
            'append_checklist_at_end': True,
            'target_section_words': 320,
            'tone': 'concise_direct',
        },
    },
    {
        'name': '网络安全与数据安全专精版',
        'description': '聚焦双域隔离、逻辑隔离磁盘访问控制、防勒索加密、DLP 行为审计与政采等保合规响应，自动衔接公司安全与通用运维指标。',
        'biz_line': 'security',
        'source_type': 'builtin',
        'is_builtin': True,
        'is_default': False,
        'writing_instructions': (
            '1. 立足网络安全与数据安全流转专业视角，紧扣「双环境物理/逻辑隔离、文件访问防泄漏、逻辑隔离磁盘防木马控制、专用磁盘防勒索加密与密文不落地灾备」清晰阐述实现机理；\n'
            '2. 针对等保 2.0 测评级别、国密 SM2/SM3/SM4 支持或第三方检测编号，仅引用公司台账或招标已明确内容，其余严禁捏造，统一留 `[待人工确认：...]` 槽位；\n'
            '3. 售后应急响应严格对照公司核准基线（7×24 电话/5×8 支持、故障响应 ≤1 小时、到场 ≤24 小时及同规格备机代用）组织运维矩阵表；\n'
            '4. 严禁虚构合同金额、过往客户名称及未授权业绩日期。'
        ),
        'layout_spec': {
            'cover_style': 'tech_modern',
            'font_zh': '微软雅黑',
            'heading_font_zh': '黑体',
            'font_size_pt': 10.5,
            'line_spacing': 1.35,
            'accent_color_hex': '1E3A8A',
            'table_header_bg': 'DBEAFE',
            'include_toc': True,
            'append_checklist_at_end': False,
            'target_section_words': 650,
            'tone': 'technical_deep',
        },
    },
    {
        'name': '并行MaaS算力服务专精版',
        'description': '聚焦大模型多模型网关路由、Token 输入/输出/缓存计量、分布式推理集群、数据不出域与知识库 RAG 对接的专精撰写规范。',
        'biz_line': 'maas',
        'source_type': 'builtin',
        'is_builtin': True,
        'is_default': False,
        'writing_instructions': (
            '1. 立足「并行 MaaS 平台 / AI 智算云平台」大模型算力与服务视角，重点突出多模型统一 OpenAI 兼容网关、DeepSeek/GLM/Qwen/Llama 等长上下文（最高 1M）调度及本地/云端双模平滑切换；\n'
            '2. Token 计量与计费机制仅说明「输入/输出/上下文缓存分项计量、账户账单可审计、额度预警与预付续购」逻辑；一切 Token 单价、折扣比例与采购金额严禁写死数字，统一用 `[待人工填报：...]` 槽位锁定；\n'
            '3. 对 API 可用性百分比、并发 RPM/TPM/QPS 与首字延迟 TTFT 等尚未签署的硬跑分参数，务必保留 `[待人工确认：...]` 槽位，同时充分写实客户内容知识产权归属、保密责任与私有化数据不出域机制；\n'
            '4. 紧密对应「知识库扩展 / RAG / 工作流编排 / 售后 7×24 与 1 小时响应」拿满技术评分。'
        ),
        'layout_spec': {
            'cover_style': 'tech_modern',
            'font_zh': '微软雅黑',
            'heading_font_zh': '黑体',
            'font_size_pt': 10.5,
            'line_spacing': 1.35,
            'accent_color_hex': '312E81',
            'table_header_bg': 'E0E7FF',
            'include_toc': True,
            'append_checklist_at_end': False,
            'target_section_words': 650,
            'tone': 'technical_deep',
        },
    },
)


class SkillImportError(RuntimeError):
    """Skill 导入失败结构化异常（带明确 error_code 供路由透传）。"""

    def __init__(self, code: str, message: str, detail: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


def normalize_layout_spec(raw: Any) -> dict[str, Any]:
    """校验并钳位 Skill 版式规范字典，防止非法字体/颜色/行距写入或注入 docx XML。"""
    spec = dict(DEFAULT_LAYOUT_SPEC)
    if not isinstance(raw, dict):
        return spec

    cs = str(raw.get('cover_style') or '').strip()
    if cs in _VALID_COVER_STYLES:
        spec['cover_style'] = cs

    tone = str(raw.get('tone') or '').strip()
    if tone in _VALID_TONES:
        spec['tone'] = tone

    for fk in ('font_zh', 'heading_font_zh'):
        fv = str(raw.get(fk) or '').strip()
        if fv in _VALID_FONTS:
            spec[fk] = fv

    try:
        fs = float(raw.get('font_size_pt', spec['font_size_pt']))
        spec['font_size_pt'] = round(max(9.0, min(16.0, fs)), 1)
    except (TypeError, ValueError):
        pass

    try:
        ls = float(raw.get('line_spacing', spec['line_spacing']))
        spec['line_spacing'] = round(max(1.0, min(2.2, ls)), 2)
    except (TypeError, ValueError):
        pass

    for ck in ('accent_color_hex', 'table_header_bg'):
        cv = str(raw.get(ck) or '').strip().lstrip('#')
        if _HEX_COLOR_RE.match(cv):
            spec[ck] = cv.upper()

    for bk in ('include_toc', 'append_checklist_at_end'):
        if bk in raw and isinstance(raw[bk], bool):
            spec[bk] = raw[bk]

    try:
        tw = int(raw.get('target_section_words', spec['target_section_words']))
        spec['target_section_words'] = max(150, min(2000, tw))
    except (TypeError, ValueError):
        pass

    return spec


def parse_skill_markdown_or_json(
    raw_text: str,
    *,
    source_name: str = '',
    default_source_type: str = 'custom',
    source_url: Optional[str] = None,
) -> dict[str, Any]:
    """解析 SKILL.md（YAML frontmatter + 正文）、普通 Markdown 或 JSON Skill 定义。

    安全规则：纯文本规则解析，绝不执行任何代码块或外部命令。
    """
    text = (raw_text or '').strip()
    if not text:
        raise SkillImportError('INVALID_SKILL_FORMAT', 'Skill 文件或文本内容为空')
    if len(text) > 128_000:
        text = text[:128_000]

    # 1. JSON 格式尝试
    if text.startswith('{'):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SkillImportError('INVALID_SKILL_FORMAT', f"Skill JSON 解析失败：{exc}") from exc
        if not isinstance(obj, dict):
            raise SkillImportError('INVALID_SKILL_FORMAT', 'Skill JSON 根节点必须为对象')
        name = str(obj.get('name') or '').strip()[:120]
        instructions = str(
            obj.get('writing_instructions') or obj.get('instructions') or obj.get('prompt') or ''
        ).strip()
        if not instructions:
            raise SkillImportError('INVALID_SKILL_FORMAT', 'Skill JSON 缺少 writing_instructions/instructions 内容')
        desc = str(obj.get('description') or '').strip()[:400]
        layout = normalize_layout_spec(obj.get('layout_spec') or obj.get('layout') or {})
        return {
            'name': name or Path(source_name or '导入写作Skill').stem[:120],
            'description': desc or instructions[:120],
            'biz_line': normalize_skill_biz_line(obj.get('biz_line')),
            'source_type': default_source_type,
            'source_url': source_url,
            'writing_instructions': instructions[:16_000],
            'layout_spec': layout,
        }

    # 2. Markdown + 可选 YAML frontmatter (--- ... ---)
    meta: dict[str, Any] = {}
    body = text
    if text.startswith('---'):
        m = re.match(r'^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n(.*))?$', text, flags=re.DOTALL)
        if m:
            fm_raw, body_raw = m.group(1), m.group(2) or ''
            try:
                loaded = yaml.safe_load(fm_raw)
                if isinstance(loaded, dict):
                    meta = loaded
            except Exception as exc:
                raise SkillImportError('INVALID_SKILL_FORMAT', f"SKILL.md frontmatter YAML 解析失败：{exc}") from exc
            body = body_raw.strip()

    # 若正文无内容但 frontmatter 里带 instructions
    if not body:
        body = str(meta.get('writing_instructions') or meta.get('instructions') or meta.get('description') or '').strip()
    if not body:
        raise SkillImportError('INVALID_SKILL_FORMAT', 'SKILL.md 正文写作指令为空')

    name = str(meta.get('name') or '').strip()
    if not name:
        h_m = re.search(r'^#\s+(.+)$', body, flags=re.MULTILINE)
        if h_m:
            name = h_m.group(1).strip()
    if not name:
        name = Path(source_name or '自定义标书Skill').stem

    desc = str(meta.get('description') or '').strip()
    if not desc:
        for line in body.splitlines():
            clean_l = line.strip().lstrip('#>*- ').strip()
            if len(clean_l) >= 6:
                desc = clean_l[:200]
                break

    layout_raw: dict[str, Any] = {}
    for k in ('layout_spec', 'layout', 'format'):
        if isinstance(meta.get(k), dict):
            layout_raw.update(meta[k])
    for lk in DEFAULT_LAYOUT_SPEC:
        if lk in meta and lk not in layout_raw:
            layout_raw[lk] = meta[lk]

    return {
        'name': name[:120],
        'description': desc[:400],
        'biz_line': normalize_skill_biz_line(meta.get('biz_line')),
        'source_type': default_source_type,
        'source_url': source_url,
        'writing_instructions': body[:16_000],
        'layout_spec': normalize_layout_spec(layout_raw),
    }


def _assert_safe_git_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise SkillImportError('UNSAFE_SKILL_URL', '仅允许使用 https:// 协议访问 GitHub / Gitee 仓库')
    if parsed.username or parsed.password:
        raise SkillImportError('UNSAFE_SKILL_URL', '链接中禁止包含认证凭据')
    host = (parsed.hostname or '').lower()
    if host not in ALLOWED_GIT_HOSTS:
        raise SkillImportError(
            'UNSAFE_SKILL_URL',
            f"出于安全隔离禁止访问非托管域名 {host!r}，仅支持 GitHub 与 Gitee 官方域名",
        )
    return url


def _build_candidate_fetch_urls(
    url_or_repo: str,
    *,
    allow_gitee_fallback: bool = True,
) -> list[tuple[str, str, str]]:
    """把用户输入转为有序待尝试的 (provider, raw_url, display_source_url) 候选列表。"""
    raw = (url_or_repo or '').strip()
    if not raw:
        raise SkillImportError('INVALID_SKILL_URL', '请输入 GitHub / Gitee 链接或 owner/repo 简写')

    candidates: list[tuple[str, str, str]] = []

    if not raw.startswith('http://') and not raw.startswith('https://'):
        m = _SHORTHAND_REPO_RE.match(raw)
        if not m:
            raise SkillImportError(
                'INVALID_SKILL_URL',
                '无法识别的仓库格式；请填写 https://github.com/... 或 owner/repo',
            )
        owner, repo, subpath = m.group(1), m.group(2), (m.group(3) or '').strip('/')
        sub_cands = [subpath] if subpath else ['SKILL.md', 'skills/SKILL.md', 'README.md']
        for br in ('main', 'master'):
            for sp in sub_cands:
                gh_raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{br}/{sp}"
                candidates.append(('github', gh_raw, f"https://github.com/{owner}/{repo}/blob/{br}/{sp}"))
        if allow_gitee_fallback:
            for br in ('master', 'main'):
                for sp in sub_cands:
                    gt_raw = f"https://gitee.com/{owner}/{repo}/raw/{br}/{sp}"
                    candidates.append(('gitee', gt_raw, f"https://gitee.com/{owner}/{repo}/blob/{br}/{sp}"))
        return candidates

    _assert_safe_git_url(raw)
    parsed = urlparse(raw)
    host = (parsed.hostname or '').lower()
    path_parts = [p for p in parsed.path.split('/') if p]

    if host == 'raw.githubusercontent.com':
        candidates.append(('github', raw, raw))
        if allow_gitee_fallback and len(path_parts) >= 4:
            owner, repo, branch = path_parts[0], path_parts[1], path_parts[2]
            fpath = '/'.join(path_parts[3:])
            gt_raw = f"https://gitee.com/{owner}/{repo}/raw/{branch}/{fpath}"
            candidates.append(('gitee', gt_raw, raw))
        return candidates

    if host == 'github.com' and len(path_parts) >= 2:
        owner, repo = path_parts[0], path_parts[1].removesuffix('.git')
        if len(path_parts) >= 5 and path_parts[2] in ('blob', 'raw'):
            branch = path_parts[3]
            fpath = '/'.join(path_parts[4:])
            gh_raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{fpath}"
            candidates.append(('github', gh_raw, raw))
            if allow_gitee_fallback:
                candidates.append(('gitee', f"https://gitee.com/{owner}/{repo}/raw/{branch}/{fpath}", raw))
            return candidates

        # 仓库根目录或 tree/branch/subpath
        subpath = ''
        branch_list = ('main', 'master')
        if len(path_parts) >= 4 and path_parts[2] == 'tree':
            branch_list = (path_parts[3],)
            subpath = '/'.join(path_parts[4:])
        sub_cands = (
            [f"{subpath}/SKILL.md".strip('/'), subpath]
            if subpath and not subpath.endswith(('.md', '.json'))
            else ([subpath] if subpath else ['SKILL.md', 'skills/SKILL.md', 'README.md'])
        )
        for br in branch_list:
            for sp in sub_cands:
                candidates.append(('github', f"https://raw.githubusercontent.com/{owner}/{repo}/{br}/{sp}", raw))
        if allow_gitee_fallback:
            for br in ('master', 'main') if len(branch_list) > 1 else branch_list:
                for sp in sub_cands:
                    candidates.append(('gitee', f"https://gitee.com/{owner}/{repo}/raw/{br}/{sp}", raw))
        return candidates

    if host == 'gitee.com' and len(path_parts) >= 2:
        owner, repo = path_parts[0], path_parts[1].removesuffix('.git')
        if len(path_parts) >= 5 and path_parts[2] in ('blob', 'raw'):
            branch = path_parts[3]
            fpath = '/'.join(path_parts[4:])
            candidates.append(('gitee', f"https://gitee.com/{owner}/{repo}/raw/{branch}/{fpath}", raw))
            return candidates
        for br in ('master', 'main'):
            for sp in ('SKILL.md', 'skills/SKILL.md', 'README.md'):
                candidates.append(('gitee', f"https://gitee.com/{owner}/{repo}/raw/{br}/{sp}", raw))
        return candidates

    raise SkillImportError('INVALID_SKILL_URL', '链接路径不完整，请提供具体的 GitHub/Gitee 仓库或 SKILL.md 文件链接')


def import_skill_from_git_url(
    url_or_repo: str,
    *,
    allow_gitee_fallback: bool = True,
    timeout: float = 12.0,
) -> dict[str, Any]:
    """从 GitHub / Gitee 拉取并解析纯文本 Skill 规则（尊重系统 TUN/代理，失败优雅降级）。"""
    candidates = _build_candidate_fetch_urls(url_or_repo, allow_gitee_fallback=allow_gitee_fallback)
    headers = {
        'User-Agent': 'BidGuard-Agent-SkillImporter/1.0',
        'Accept': 'text/plain, text/markdown, application/json, */*',
    }

    session = requests.Session()
    network_errors: list[str] = []
    primary_network_failed = False
    not_found_count = 0

    for provider, raw_url, disp_url in candidates:
        _assert_safe_git_url(raw_url)
        try:
            resp = session.get(raw_url, headers=headers, timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            network_errors.append(f"{provider}: {type(exc).__name__}")
            if provider == 'github':
                primary_network_failed = True
            continue

        # 校验重定向目标域名未跳出安全白名单
        if resp.url:
            _assert_safe_git_url(resp.url)

        if resp.status_code == 200 and resp.text and resp.text.strip():
            ct = (resp.headers.get('Content-Type') or '').lower()
            # Gitee/GitHub 某些错误路径会回 HTML 登录或 404 壳页，显式拒收
            if 'text/html' in ct and resp.text.lstrip().lower().startswith('<!doctype html'):
                not_found_count += 1
                continue
            parsed = parse_skill_markdown_or_json(
                resp.text,
                source_name=Path(urlparse(raw_url).path).name or 'SKILL.md',
                default_source_type=provider,
                source_url=disp_url,
            )
            parsed['fetched_from_provider'] = provider
            parsed['used_fallback'] = (provider == 'gitee' and 'github' in url_or_repo.lower())
            return parsed

        not_found_count += 1

    if network_errors and (not_found_count == 0 or primary_network_failed):
        raise SkillImportError(
            'EXTERNAL_SKILL_UNREACHABLE',
            (
                '无法连接 GitHub（或对应托管源），已自动降级检查 Gitee 亦未获取有效文件。'
                '系统支持直接上传本地 .md/.json 文件或表单创建 Skill，外网拉取绝非功能前置依赖。'
            ),
            detail={'errors': network_errors[:4]},
        )

    raise SkillImportError(
        'SKILL_FILE_NOT_FOUND',
        (
            '未在目标仓库/分支找到有效的 SKILL.md 或 JSON Skill 文件（已尝试 GitHub 与 Gitee 常见分支及路径）。'
            '请确认文件路径，或改用本地上传 .md/.json 文件导入。'
        ),
        detail={'network_errors': network_errors[:4], 'tried_count': len(candidates)},
    )
