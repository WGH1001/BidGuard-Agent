# -*- coding: utf-8 -*-
"""E4 · 法规官网静态页更新适配器（core/regulation_crawler.py）。

职责与红线（对应 E4 任务卡 §1 ~ §3）：
1. 读取 config/regulation_sources.yaml 源列表；
2. 若某源的 url 为空或以 TODO_USER_FILL 开头，诚实返回 status='unconfigured'（"源未配置"），绝不抛 500；
3. static_html 适配器：
   - 使用 requests 拉取 URL（timeout=15s，携带浏览器 User-Agent，失败自动重试 1 次）；
   - 使用 lxml 提取正文文本并调用 split_text_into_articles 按"第X条"正则切条；
   - 若页面为 JS 动态渲染壳子或切出条文数 < 2，诚实返回 status='manual_required'（"该源需手动导入"），严禁静默吞错或用 LLM 编造；
   - 与数据库内已有法规的 content_hash 比对，仅当存在差异时更新入库并返回差异明细报告。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, Optional

import lxml.html
import requests
from sqlalchemy.orm import Session
import yaml

from backend.models import RegulationLaw
from core.regulation_library import (
    normalize_article_no,
    split_text_into_articles,
    upsert_law_articles,
)

_DEFAULT_SOURCES_YAML = Path(__file__).resolve().parents[1] / 'config' / 'regulation_sources.yaml'
_DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}


def load_regulation_sources(yaml_path: Optional[str | Path] = None) -> list[dict[str, str]]:
    """读取 config/regulation_sources.yaml 配置的官网源清单。"""
    target = Path(yaml_path) if yaml_path is not None else _DEFAULT_SOURCES_YAML
    if not target.is_file():
        return []
    raw = yaml.safe_load(target.read_text(encoding='utf-8')) or {}
    sources = raw.get('sources') if isinstance(raw, dict) else []
    if not isinstance(sources, list):
        return []
    out: list[dict[str, str]] = []
    for item in sources:
        if isinstance(item, dict) and item.get('name'):
            out.append({
                'name': str(item.get('name') or '').strip(),
                'url': str(item.get('url') or '').strip(),
                'adapter': str(item.get('adapter') or 'static_html').strip(),
            })
    return out


def _fetch_html_with_retry(
    url: str,
    http_get: Optional[Callable[..., Any]] = None,
    timeout: int = 15,
) -> str:
    """拉取静态网页 HTML，超时 15s、UA 伪装、失败自动重试 1 次。"""
    getter = http_get or requests.get
    last_exc: Optional[Exception] = None
    for _attempt in range(2):
        try:
            resp = getter(url, headers=_DEFAULT_HEADERS, timeout=timeout)
            status_code = getattr(resp, 'status_code', 200)
            if status_code >= 400:
                raise RuntimeError(f"HTTP {status_code}")
            # 优先处理中文网页编码
            if hasattr(resp, 'apparent_encoding') and getattr(resp, 'encoding', '').lower() in ('iso-8859-1', ''):
                resp.encoding = resp.apparent_encoding or 'utf-8'
            text = getattr(resp, 'text', '')
            if not text and hasattr(resp, 'content'):
                text = resp.content.decode('utf-8', errors='replace')
            return str(text or '')
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"请求官网源失败（已重试 1 次）：{last_exc}") from last_exc


def _extract_plain_text_from_html(html_str: str) -> str:
    """使用 lxml.html 剥离 script/style 标签并按块级元素换行提取纯文本。"""
    if not html_str or not html_str.strip():
        return ''
    try:
        doc = lxml.html.fromstring(html_str)
        for bad in doc.xpath('//script | //style | //noscript'):
            parent = bad.getparent()
            if parent is not None:
                parent.remove(bad)
        for br in doc.xpath('//br | //p | //div | //li | //tr | //h1 | //h2 | //h3 | //h4'):
            br.tail = '\n' + (br.tail or '')
        return doc.text_content()
    except Exception:
        return ''


def crawl_single_source(
    session: Session,
    source: dict[str, str],
    http_get: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """对单个配置源执行拉取、切条、content_hash 比对与差异入库。"""
    raw_name = str(source.get('name') or '').strip()
    url = str(source.get('url') or '').strip()
    adapter = str(source.get('adapter') or 'static_html').strip()

    law_title = raw_name if raw_name.startswith('《') else f"《{raw_name}》"

    # 1. TODO_USER_FILL 占位源诚实报"源未配置"
    if not url or url.upper().startswith('TODO_USER_FILL'):
        return {
            'name': law_title,
            'url': url or 'TODO_USER_FILL',
            'status': 'unconfigured',
            'updated': False,
            'article_count': 0,
            'added_count': 0,
            'modified_count': 0,
            'unchanged_count': 0,
            'message': '源未配置（请在 config/regulation_sources.yaml 中填写官方公布页 URL）',
        }

    if adapter != 'static_html':
        return {
            'name': law_title,
            'url': url,
            'status': 'unsupported_adapter',
            'updated': False,
            'article_count': 0,
            'added_count': 0,
            'modified_count': 0,
            'unchanged_count': 0,
            'message': f"不支持的适配器类型: {adapter}，该源需手动导入",
        }

    # 2. 拉取静态 HTML（超时 15s + 重试 1 次）
    try:
        html_str = _fetch_html_with_retry(url, http_get=http_get, timeout=15)
    except Exception as exc:
        return {
            'name': law_title,
            'url': url,
            'status': 'fetch_error',
            'updated': False,
            'article_count': 0,
            'added_count': 0,
            'modified_count': 0,
            'unchanged_count': 0,
            'message': f"官网拉取失败（{exc}），该源需手动导入",
        }

    # 3. lxml 提取正文并按"第X条"切条
    plain_text = _extract_plain_text_from_html(html_str)
    articles = split_text_into_articles(plain_text)
    if len(articles) < 2:
        return {
            'name': law_title,
            'url': url,
            'status': 'manual_required',
            'updated': False,
            'article_count': 0,
            'added_count': 0,
            'modified_count': 0,
            'unchanged_count': 0,
            'message': '该源需手动导入（页面为 JS 动态渲染或未解析出有效法条正文）',
        }

    # 4. 计算整部法规 content_hash 并与库内比对
    valid_hashes = []
    for a in articles:
        std_no = normalize_article_no(a['article'])
        c_hash = hashlib.sha256(a['content'].encode('utf-8')).hexdigest()
        valid_hashes.append(f"{std_no}:{c_hash}")
    new_law_hash = hashlib.sha256('\n'.join(valid_hashes).encode('utf-8')).hexdigest()

    existing_law = session.query(RegulationLaw).filter_by(name=law_title).first()
    if existing_law is not None and existing_law.content_hash == new_law_hash:
        return {
            'name': law_title,
            'url': url,
            'status': 'unchanged',
            'updated': False,
            'article_count': existing_law.article_count,
            'added_count': 0,
            'modified_count': 0,
            'unchanged_count': existing_law.article_count,
            'message': '与库内版本一致（content_hash 无变化）',
        }

    # 5. 有差异时执行入库更新
    diff_res = upsert_law_articles(
        session,
        law_name=law_title,
        articles=articles,
        source_url=url,
        is_seed=False,
    )
    return {
        'name': law_title,
        'url': url,
        'status': 'updated',
        'updated': True,
        'article_count': diff_res['article_count'],
        'added_count': diff_res['added_count'],
        'modified_count': diff_res['modified_count'],
        'unchanged_count': diff_res['unchanged_count'],
        'message': (
            f"更新完成：共 {diff_res['article_count']} 条"
            f"（新增 {diff_res['added_count']} 条，修订 {diff_res['modified_count']} 条）"
        ),
    }


def crawl_all_configured_sources(
    session: Session,
    yaml_path: Optional[str | Path] = None,
    http_get: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """遍历 config/regulation_sources.yaml 全部源并返回汇总报告。"""
    sources = load_regulation_sources(yaml_path)
    results = [crawl_single_source(session, src, http_get=http_get) for src in sources]
    updated_laws = sum(1 for r in results if r['updated'])
    unconfigured = sum(1 for r in results if r['status'] == 'unconfigured')
    return {
        'total_sources': len(results),
        'updated_laws_count': updated_laws,
        'unconfigured_count': unconfigured,
        'results': results,
    }