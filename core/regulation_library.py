# -*- coding: utf-8 -*-
"""E4 · 招投标轻量法规库与法条三态真伪核验核心引擎（core/regulation_library.py）。

设计铁律（对应 E4 任务卡 §1 & §2）：
1. normalize_article_text(text)：全半角转换、空白折叠、标点归一、条款号变体归一（"第1条" == "第一条"）；
2. verify_article(text, session=None, client=None) -> dict 三态判定（本卡灵魂）：
   - exact_match：归一化后与库内某条逐字相等（或含该条完整正文且条号一致），返回 law/article_no/official_content；
   - fuzzy_hint：归一化不完全相等但 BM25+序列重合度 >= 阈值（或条号张冠李戴），返回"疑似系《X》第X条的改写"提示 + 相似度；
     LLM 在真伪判定中零裁决权——仅允许辅助解释差异点且输出强制带"未经核验，仅供参考"角标；
   - unverifiable：库外且无近似，明确标注"库中未收录，真伪未经核验"；
3. 支持 json / txt / docx / pdf 四种格式导入（PDF 走 core.parser.parse_document 含扫描件 RapidOCR 链路），
   按"第X条"立法正则切分条文并计算 SHA256 content_hash 幂等入库。
"""
from __future__ import annotations

from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Optional

from sqlalchemy.orm import Session

from core.hybrid_retriever import BM25Index
from core.parser import parse_document

_CN_DIGIT_MAP = {
    '零': 0, '〇': 0, '一': 1, '壹': 1, '二': 2, '贰': 2, '两': 2,
    '三': 3, '叁': 3, '四': 4, '肆': 4, '五': 5, '伍': 5,
    '六': 6, '陆': 6, '七': 7, '柒': 7, '八': 8, '捌': 8, '九': 9, '玖': 9,
}
_CN_UNIT_MAP = {'十': 10, '拾': 10, '百': 100, '佰': 100, '千': 1000, '仟': 1000}

_ARTICLE_TOKEN_RE = re.compile(r'第\s*([零〇一二三四五六七八九十百千壹贰叁肆伍陆柒捌玖拾佰仟两0-9]{1,8})\s*条')
_LAW_TITLE_PREFIX_RE = re.compile(r'^\s*《([^》]{2,60})》\s*')
_SPLIT_ARTICLE_LINE_RE = re.compile(
    r'(?:^|\n)\s*(第\s*[零〇一二三四五六七八九十百千0-9]{1,7}\s*条)(?=[\s\u3000:：、.．]|[^\n])'
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cn_num_to_int(s: str) -> Optional[int]:
    """将中文数字或阿拉伯数字字符串转为正整数（如 '一'->1, '二十八'->28, '一百零三'->103, '68'->68）。"""
    clean = (s or '').strip()
    if not clean:
        return None
    if clean.isdigit():
        val = int(clean)
        return val if val > 0 else None

    total = 0
    current = 0
    for ch in clean:
        if ch in _CN_DIGIT_MAP:
            current = _CN_DIGIT_MAP[ch]
        elif ch in _CN_UNIT_MAP:
            unit = _CN_UNIT_MAP[ch]
            if current == 0:
                current = 1
            total += current * unit
            current = 0
        else:
            return None
    result = total + current
    return result if result > 0 else None


def int_to_cn_num(n: int) -> str:
    """将 1~9999 的正整数转为标准立法中文数字（如 1->'一', 12->'十二', 28->'二十八', 105->'一百零五'）。"""
    if n <= 0 or n >= 10000:
        return str(n)
    digits = '零一二三四五六七八九'
    if n < 10:
        return digits[n]
    if n < 20:
        return '十' + (digits[n % 10] if n % 10 else '')
    if n < 100:
        tens, ones = divmod(n, 10)
        return f"{digits[tens]}十{digits[ones] if ones else ''}"
    if n < 1000:
        hundreds, rem = divmod(n, 100)
        if rem == 0:
            return f"{digits[hundreds]}百"
        if rem < 10:
            return f"{digits[hundreds]}百零{digits[rem]}"
        tens, ones = divmod(rem, 10)
        return f"{digits[hundreds]}百{digits[tens]}十{digits[ones] if ones else ''}"
    thousands, rem = divmod(n, 1000)
    if rem == 0:
        return f"{digits[thousands]}千"
    if rem < 100:
        return f"{digits[thousands]}千零{int_to_cn_num(rem)}"
    return f"{digits[thousands]}千{int_to_cn_num(rem)}"


def normalize_article_no(raw_no: str) -> str:
    """将任意条文号写法（'第1条' / '第一条' / '1' / '第 28 条'）统一为标准中文 '第X条'。"""
    s = (raw_no or '').strip()
    if not s:
        return ''
    m = _ARTICLE_TOKEN_RE.search(s)
    token = m.group(1) if m else s.lstrip('第').rstrip('条').strip()
    num = cn_num_to_int(token)
    if num is not None:
        return f"第{int_to_cn_num(num)}条"
    return f"第{token}条" if not s.startswith('第') else re.sub(r'\s+', '', s)


def normalize_article_text(text: str) -> str:
    """E4 核心函数 ①：法条文本归一化（全半角、空白折叠、条款号变体"第1条"=="第一条"归一）。"""
    if not text:
        return ''

    half_chars: list[str] = []
    for ch in str(text):
        code = ord(ch)
        if code == 0x3000:
            half_chars.append(' ')
        elif 0xFF01 <= code <= 0xFF5E:
            half_chars.append(chr(code - 0xFEE0))
        else:
            half_chars.append(ch)
    s = ''.join(half_chars)

    def _repl_art_no(m: re.Match) -> str:
        val = cn_num_to_int(m.group(1))
        return f"第{val}条" if val is not None else m.group(0)

    s = _ARTICLE_TOKEN_RE.sub(_repl_art_no, s)
    s = re.sub(r'[\s\u3000，,。．.；;：:？?！!“”"\'‘’（）()【】\[\]《》〈〉—\-·…]+', '', s)
    return s.lower()


def _extract_input_law_and_article_no(raw_text: str) -> tuple[Optional[str], Optional[str], str]:
    """从用户输入法条文本中分离可选的《法律名称》、第X条编号与纯条文正文。"""
    txt = (raw_text or '').strip()
    law_hint: Optional[str] = None
    art_no_hint: Optional[str] = None

    m_law = _LAW_TITLE_PREFIX_RE.match(txt)
    if m_law:
        law_hint = f"《{m_law.group(1).strip()}》"
        txt = txt[m_law.end():].strip()

    m_art = re.match(
        r'^第\s*([零〇一二三四五六七八九十百千壹贰叁肆伍陆柒捌玖拾佰仟两0-9]{1,8})\s*条[\s:：、.．—\-]*',
        txt,
    )
    if m_art:
        art_no_hint = normalize_article_no(m_art.group(0))
        body = txt[m_art.end():].strip()
    else:
        body = txt

    return law_hint, art_no_hint, body


def _get_active_session(session: Optional[Session] = None) -> tuple[Session, bool]:
    if session is not None:
        return session, False
    from backend.database import SessionLocal
    return SessionLocal(), True


def verify_article(
    text: str,
    session: Optional[Session] = None,
    client: Any = None,
    fuzzy_threshold: float = 0.35,
) -> dict[str, Any]:
    """E4 核心函数 ②：法条三态真伪核验（exact_match / fuzzy_hint / unverifiable）。"""
    raw_input = (text or '').strip()
    if not raw_input or len(normalize_article_text(raw_input)) < 4:
        return {
            'status': 'unverifiable',
            'verified': False,
            'law': None,
            'article_no': None,
            'official_content': None,
            'similarity': 0.0,
            'badge': '库中未收录，真伪未经核验',
            'message': '库中未收录，真伪未经核验',
        }

    from backend.models import RegulationArticle, RegulationLaw

    db, own_db = _get_active_session(session)
    try:
        rows = (
            db.query(RegulationArticle, RegulationLaw.name)
            .join(RegulationLaw, RegulationArticle.law_id == RegulationLaw.id)
            .all()
        )
        if not rows:
            return {
                'status': 'unverifiable',
                'verified': False,
                'law': None,
                'article_no': None,
                'official_content': None,
                'similarity': 0.0,
                'badge': '库中未收录，真伪未经核验',
                'message': '库中未收录，真伪未经核验',
            }

        law_hint, art_no_hint, input_body = _extract_input_law_and_article_no(raw_input)
        norm_full_input = normalize_article_text(raw_input)
        norm_body_input = normalize_article_text(input_body) if input_body else norm_full_input

        mismatched_no_candidate: Optional[tuple[RegulationArticle, str]] = None

        for art_obj, law_name in rows:
            if law_hint and law_hint != law_name and law_hint.strip('《》') not in law_name:
                continue

            norm_art_body = normalize_article_text(art_obj.content)
            norm_art_with_no = normalize_article_text(f"{art_obj.article_no} {art_obj.content}")
            norm_art_with_law = normalize_article_text(f"{law_name}{art_obj.article_no}{art_obj.content}")

            body_exact = bool(norm_body_input and norm_body_input == norm_art_body)
            full_exact = norm_full_input in (norm_art_body, norm_art_with_no, norm_art_with_law)

            if body_exact or full_exact:
                if art_no_hint and normalize_article_no(art_obj.article_no) != art_no_hint:
                    mismatched_no_candidate = (art_obj, law_name)
                    continue

                return {
                    'status': 'exact_match',
                    'verified': True,
                    'law': law_name,
                    'article_no': art_obj.article_no,
                    'official_content': art_obj.content,
                    'similarity': 1.0,
                    'badge': f"库内原文（{law_name}{art_obj.article_no}）",
                    'message': f"核验通过：与库内{law_name}{art_obj.article_no}原文逐字一致",
                }

        if mismatched_no_candidate is not None:
            art_obj, law_name = mismatched_no_candidate
            return {
                'status': 'fuzzy_hint',
                'verified': False,
                'law': law_name,
                'article_no': art_obj.article_no,
                'official_content': art_obj.content,
                'similarity': 0.95,
                'badge': f"疑似系{law_name}{art_obj.article_no}改写，未经核验",
                'message': (
                    f"疑似系{law_name}{art_obj.article_no}的改写（正文与{art_obj.article_no}一致，"
                    f"但输入条号为{art_no_hint}），未经核验，仅供参考"
                ),
            }

        chunks: list[dict[str, Any]] = []
        for idx, (art_obj, law_name) in enumerate(rows):
            full_doc_str = f"{law_name} {art_obj.article_no} {art_obj.content}"
            n_body = normalize_article_text(art_obj.content)
            bigrams = [n_body[i:i + 2] for i in range(max(0, len(n_body) - 1))] if len(n_body) >= 2 else [n_body]
            chunks.append({
                'idx': idx,
                'law': law_name,
                'article_no': art_obj.article_no,
                'content': art_obj.content,
                'norm_body': n_body,
                'text': full_doc_str,
                'window': full_doc_str,
                'start': 0,
                'end': len(full_doc_str),
                'tokens': bigrams + [normalize_article_text(art_obj.article_no)],
            })

        bm25 = BM25Index(chunks)
        q_norm = norm_body_input or norm_full_input
        q_bigrams = ' '.join(q_norm[i:i + 2] for i in range(max(0, len(q_norm) - 1)))
        bm25_hits = bm25.search(f"{raw_input} {q_bigrams}", top_k=10)

        best_sim = 0.0
        best_chunk: Optional[dict[str, Any]] = None

        candidate_indices = {
            c['idx'] for c in chunks
            if (art_no_hint and normalize_article_no(c['article_no']) == art_no_hint)
        }
        for h in bm25_hits:
            for c in chunks:
                if c['text'] == h['quote']:
                    candidate_indices.add(c['idx'])
                    break

        if not candidate_indices:
            candidate_indices = set(range(min(len(chunks), 30)))

        q_set = set(q_norm[i:i + 2] for i in range(max(0, len(q_norm) - 1))) if len(q_norm) >= 2 else {q_norm}

        for c_idx in candidate_indices:
            c = chunks[c_idx]
            target_norm = c['norm_body']
            if not target_norm or not q_norm:
                continue
            seq_ratio = difflib.SequenceMatcher(None, q_norm, target_norm, autojunk=False).ratio()
            t_set = set(target_norm[i:i + 2] for i in range(max(0, len(target_norm) - 1)))
            jaccard = len(q_set & t_set) / max(1, len(q_set | t_set))
            containment = len(q_set & t_set) / max(1, min(len(q_set), len(t_set)))
            sim = max(seq_ratio, 0.55 * seq_ratio + 0.45 * containment, jaccard)
            if law_hint and (law_hint == c['law'] or law_hint.strip('《》') in c['law']):
                sim = min(0.99, sim + 0.08)
            if sim > best_sim:
                best_sim = sim
                best_chunk = c

        if best_chunk is not None and best_sim >= fuzzy_threshold:
            b_law = best_chunk['law']
            b_no = best_chunk['article_no']
            b_content = best_chunk['content']
            sim_val = round(float(best_sim), 4)

            llm_hint = None
            if client is not None:
                try:
                    prompt = (
                        f"请用 30 字以内简要指出【待核验文本】与【官方库内条文】的字面差异点（严禁判定真伪）：\n"
                        f"【待核验文本】{raw_input}\n"
                        f"【官方库内条文】{b_law}{b_no}：{b_content}"
                    )
                    rsp = client.chat([{'role': 'user', 'content': prompt}], model_tier='flash')
                    raw_hint = str(getattr(rsp, 'content', '') or '').strip()
                    if raw_hint:
                        llm_hint = f"{raw_hint}（未经核验，仅供参考）"
                except Exception:
                    llm_hint = None

            return {
                'status': 'fuzzy_hint',
                'verified': False,
                'law': b_law,
                'article_no': b_no,
                'official_content': b_content,
                'similarity': sim_val,
                'badge': f"疑似系{b_law}{b_no}改写，未经核验",
                'message': f"疑似系{b_law}{b_no}的改写（相似度 {round(sim_val * 100, 1)}%），未经核验，仅供参考",
                'llm_diff_hint': llm_hint,
            }

        return {
            'status': 'unverifiable',
            'verified': False,
            'law': None,
            'article_no': None,
            'official_content': None,
            'similarity': round(float(best_sim), 4),
            'badge': '库中未收录，真伪未经核验',
            'message': '库中未收录，真伪未经核验',
        }
    finally:
        if own_db:
            db.close()


def cite_regulation(
    keyword: str = '',
    article_no: str = '',
    law_name: str = '',
    session: Optional[Session] = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """E4 供 F7 ReAct Agent 挂载的第 6 工具：检索法规库返回条文原文 + 法律名 + 条号。"""
    from backend.models import RegulationArticle, RegulationLaw

    db, own_db = _get_active_session(session)
    try:
        total_laws = db.query(RegulationLaw).count()
        if total_laws == 0:
            return {
                'status': 'empty_library',
                'message': '法规库为空',
                'items': [],
            }

        query_obj = (
            db.query(RegulationArticle, RegulationLaw.name)
            .join(RegulationLaw, RegulationArticle.law_id == RegulationLaw.id)
        )
        if law_name and law_name.strip():
            clean_law = law_name.strip().strip('《》')
            query_obj = query_obj.filter(RegulationLaw.name.contains(clean_law))

        rows = query_obj.all()
        if not rows:
            return {'status': 'ok', 'message': '未找到匹配的法规条文', 'items': []}

        norm_target_no = normalize_article_no(article_no) if article_no and article_no.strip() else ''
        if norm_target_no:
            exact_no_hits = [
                {
                    'law': l_name,
                    'article_no': art.article_no,
                    'content': art.content,
                }
                for art, l_name in rows
                if normalize_article_no(art.article_no) == norm_target_no
            ]
            if exact_no_hits:
                # 多部法律存在同号条文时，按 keyword 相关性优先排序（稳定排序，同分保持原序）：
                # 如'第三十三条'同时命中招标投标法与反不正当竞争法，keyword='低于成本'
                # 必须把含关键词的招标投标法条文排到首位，避免张冠李戴。
                kw_stripped = (keyword or '').strip()
                if kw_stripped:
                    exact_no_hits.sort(
                        key=lambda it: 0 if (kw_stripped in it['content'] or kw_stripped in it['law']) else 1
                    )
                return {'status': 'ok', 'message': f"命中 {len(exact_no_hits)} 条法条", 'items': exact_no_hits[:top_k]}

        kw = (keyword or article_no or '').strip()
        if not kw:
            sample = [
                {'law': l_name, 'article_no': art.article_no, 'content': art.content}
                for art, l_name in rows[:top_k]
            ]
            return {'status': 'ok', 'message': f"返回前 {len(sample)} 条法条", 'items': sample}

        direct_hits = [
            {'law': l_name, 'article_no': art.article_no, 'content': art.content}
            for art, l_name in rows
            if kw in art.content or kw in l_name or (norm_target_no and norm_target_no == art.article_no)
        ]
        if direct_hits:
            return {'status': 'ok', 'message': f"命中 {len(direct_hits[:top_k])} 条法条", 'items': direct_hits[:top_k]}

        chunks = []
        for idx, (art, l_name) in enumerate(rows):
            txt = f"{l_name} {art.article_no} {art.content}"
            n_txt = normalize_article_text(txt)
            toks = [n_txt[i:i + 2] for i in range(max(0, len(n_txt) - 1))]
            chunks.append({
                'idx': idx,
                'law': l_name,
                'article_no': art.article_no,
                'content': art.content,
                'text': txt,
                'window': txt,
                'start': 0,
                'end': len(txt),
                'tokens': toks,
            })
        bm25 = BM25Index(chunks)
        q_norm = normalize_article_text(kw)
        q_toks = ' '.join(q_norm[i:i + 2] for i in range(max(0, len(q_norm) - 1)))
        scored = bm25.search(f"{kw} {q_toks}", top_k=top_k)
        out_items = []
        for h in scored:
            for c in chunks:
                if c['text'] == h['quote']:
                    out_items.append({
                        'law': c['law'],
                        'article_no': c['article_no'],
                        'content': c['content'],
                    })
                    break
        return {'status': 'ok', 'message': f"检索到 {len(out_items)} 条相关法条", 'items': out_items}
    finally:
        if own_db:
            db.close()


def split_text_into_articles(raw_text: str) -> list[dict[str, str]]:
    """从 txt / docx / pdf 解析出的法规全文中按 '第X条' 确定性切分条文列表。"""
    if not raw_text or not raw_text.strip():
        return []

    matches = list(_SPLIT_ARTICLE_LINE_RE.finditer(raw_text))
    if not matches:
        return []

    articles: list[dict[str, str]] = []
    seen_nos: set[str] = set()

    for i, m in enumerate(matches):
        raw_no = m.group(1)
        std_no = normalize_article_no(raw_no)
        body_start = m.end(1)
        body_end = matches[i + 1].start(1) if i + 1 < len(matches) else len(raw_text)
        body = raw_text[body_start:body_end].strip()
        body = re.sub(r'^[\s\u3000:：、.．—\-]+', '', body)
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        cleaned_lines = [
            ln for ln in lines
            if not re.match(r'^第[一二三四五六七八九十百0-9]+[章节编]\s*[^\n。；]{1,30}$', ln)
        ]
        content = ''.join(cleaned_lines).strip()
        if not std_no or len(content) < 4 or std_no in seen_nos:
            continue
        seen_nos.add(std_no)
        articles.append({
            'article': std_no,
            'content': content,
        })

    return articles


def upsert_law_articles(
    session: Session,
    law_name: str,
    articles: list[dict[str, Any]],
    *,
    issuing_body: Optional[str] = None,
    effective_date: Optional[str] = None,
    source_url: Optional[str] = None,
    is_seed: bool = False,
) -> dict[str, Any]:
    """将结构化法条列表幂等写入或更新至 regulation_laws 与 regulation_articles。"""
    from backend.models import RegulationArticle, RegulationLaw

    formatted_name = law_name.strip()
    if not formatted_name.startswith('《'):
        formatted_name = f"《{formatted_name}"
    if not formatted_name.endswith('》'):
        formatted_name = f"{formatted_name}》"

    valid_list: list[tuple[str, Optional[str], str, str]] = []
    seen_nos: set[str] = set()
    for item in articles or []:
        if not isinstance(item, dict):
            continue
        raw_no = str(item.get('article') or item.get('article_no') or '').strip()
        std_no = normalize_article_no(raw_no) if raw_no else ''
        content = str(item.get('content') or '').strip()
        title = str(item.get('title') or item.get('article_title') or '').strip() or None
        if not std_no or not content or std_no in seen_nos:
            continue
        seen_nos.add(std_no)
        c_hash = hashlib.sha256(content.encode('utf-8')).hexdigest()
        valid_list.append((std_no, title, content, c_hash))

    if not valid_list:
        raise ValueError("未能从文件中提取出任何有效的法条记录（需包含'第X条'格式条文）")

    combined = '\n'.join(f"{a[0]}:{a[3]}" for a in valid_list)
    law_hash = hashlib.sha256(combined.encode('utf-8')).hexdigest()
    now = _utc_now()

    law_row = session.query(RegulationLaw).filter_by(name=formatted_name).first()
    added_count = 0
    modified_count = 0
    unchanged_count = 0

    if law_row is None:
        law_row = RegulationLaw(
            name=formatted_name,
            issuing_body=issuing_body or '国家立法/监管机关',
            effective_date=effective_date,
            source_url=source_url,
            article_count=len(valid_list),
            content_hash=law_hash,
            fetched_at=now,
            is_seed=is_seed,
            created_at=now,
            updated_at=now,
        )
        session.add(law_row)
        session.flush()
        for std_no, title, content, c_hash in valid_list:
            session.add(
                RegulationArticle(
                    law_id=law_row.id,
                    article_no=std_no,
                    article_title=title,
                    content=content,
                    content_hash=c_hash,
                    created_at=now,
                    updated_at=now,
                )
            )
        added_count = len(valid_list)
    else:
        existing_arts = {
            a.article_no: a
            for a in session.query(RegulationArticle).filter_by(law_id=law_row.id).all()
        }
        for std_no, title, content, c_hash in valid_list:
            if std_no not in existing_arts:
                session.add(
                    RegulationArticle(
                        law_id=law_row.id,
                        article_no=std_no,
                        article_title=title,
                        content=content,
                        content_hash=c_hash,
                        created_at=now,
                        updated_at=now,
                    )
                )
                added_count += 1
            else:
                art_row = existing_arts[std_no]
                if art_row.content_hash != c_hash:
                    art_row.content = content
                    art_row.article_title = title or art_row.article_title
                    art_row.content_hash = c_hash
                    art_row.updated_at = now
                    modified_count += 1
                else:
                    unchanged_count += 1

        law_row.article_count = session.query(RegulationArticle).filter_by(law_id=law_row.id).count()
        law_row.content_hash = law_hash
        law_row.fetched_at = now
        law_row.updated_at = now
        if source_url:
            law_row.source_url = source_url

    session.commit()
    session.refresh(law_row)
    return {
        'law_id': law_row.id,
        'law_name': law_row.name,
        'article_count': law_row.article_count,
        'content_hash': law_row.content_hash,
        'added_count': added_count,
        'modified_count': modified_count,
        'unchanged_count': unchanged_count,
    }


def import_regulation_file(
    session: Session,
    file_path: str | Path,
    law_name_hint: Optional[str] = None,
    source_url: Optional[str] = None,
) -> dict[str, Any]:
    """支持 json / txt / docx / pdf 四种格式的法规文件解析与入库。"""
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext not in ('.json', '.txt', '.docx', '.pdf'):
        raise ValueError(f"不支持的法规导入格式: {ext}，仅支持 json/txt/docx/pdf")

    if ext == '.json':
        raw = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            raise ValueError("JSON 法规文件顶层必须为对象格式 {law, articles}")
        law_name = str(law_name_hint or raw.get('law') or path.stem).strip()
        articles = raw.get('articles')
        if not isinstance(articles, list) or not articles:
            raise ValueError("JSON 法规文件缺少有效的 articles 列表")
        return upsert_law_articles(
            session,
            law_name=law_name,
            articles=articles,
            source_url=source_url or f"upload://{path.name}",
        )

    parsed = parse_document(str(path))
    text = parsed.text or ''
    articles = split_text_into_articles(text)
    inferred_name = (law_name_hint or '').strip()
    if not inferred_name:
        m_book = re.search(r'《([^》]{4,40})》', text[:400])
        inferred_name = m_book.group(1) if m_book else path.stem

    return upsert_law_articles(
        session,
        law_name=inferred_name,
        articles=articles,
        source_url=source_url or f"upload://{path.name}",
    )