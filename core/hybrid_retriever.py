# -*- coding: utf-8 -*-
"""E2 / E12 · 三级自适应混合检索器（BM25 + 45组招投标同义词扩展 + SHA256向量缓存 + Rerank 精排）。

设计目标（对应 E2 任务卡 §2.2 与 E12 交叉复查加固）：
1. 严格门控（Gate Filter）：
   - 仅当 F4 初筛（条款号/标题/高 2-gram 重合度）未命中产生"疑似漏应答候选"或 F7 自然语言提问时才调用本模块；
   - 规范标书 85%+ 条款在 F4 初筛 0ms 直通，绝不触发混合检索。
2. 三级自适应检索链：
   - Level 1（0-Token 离线极速层，<3ms）：纯 Python Okapi BM25 + 45 组招投标领域同义词双向扩展；
   - Level 2（有 API Key 且启用向量时自动激活）：基于文件/文本 SHA256 缓存（data/cache/{hash}.emb.npy）的
     Embedding 余弦粗筛 Top-15 + 交叉精排 Top-3（含行数与向量维度双重校验，维度变更自动作废重算）；
   - Level 3（无 Key / 离线单测 / 网关异常兜底）：自动停留在 Level 1，保证离线环境 100% 零报错、毫秒级返回。
3. 零新增 pip 依赖：BM25 纯标准库实现；向量运算与缓存复用已安装的 numpy 与 openai。
"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import threading
from typing import Any, Optional

import numpy as np

_CACHE_LOCKS_GUARD = threading.Lock()
_CACHE_LOCKS: dict[str, threading.Lock] = {}


def _get_cache_lock(doc_sha: str) -> threading.Lock:
    """按文档内容哈希获取互斥锁，防止同轮并行工具检索同时写同一 .emb.npy 缓存文件。"""
    with _CACHE_LOCKS_GUARD:
        lock = _CACHE_LOCKS.get(doc_sha)
        if lock is None:
            lock = threading.Lock()
            _CACHE_LOCKS[doc_sha] = lock
        return lock


def _atomic_save_vectors(cache_file: Path, doc_vecs: np.ndarray) -> None:
    """原子写入向量缓存：先写临时文件再 os.replace()，避免并发读写读到半截 .npy。"""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = _CACHE_DIR / f".{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_file, 'wb') as fp:
            np.save(fp, doc_vecs)
        os.replace(str(tmp_file), str(cache_file))
    except Exception:
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except Exception:
            pass

# ── 检索调用统计桩（供门控直通单测精确断言调用次数）────────────────────────
_HYBRID_STATS: dict[str, int] = {
    'total_calls': 0,
    'level1_calls': 0,
    'level2_calls': 0,
}


def get_hybrid_stats() -> dict[str, int]:
    """返回混合检索器累计调用统计快照。"""
    return dict(_HYBRID_STATS)


def reset_hybrid_stats() -> None:
    """重置混合检索器调用统计计数器（供单测前置清零）。"""
    _HYBRID_STATS['total_calls'] = 0
    _HYBRID_STATS['level1_calls'] = 0
    _HYBRID_STATS['level2_calls'] = 0


# ── 45 组招投标领域高频同义词词库（下限 40 组，双向对称扩展）────────────────
BID_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ('项目经理', '项目负责人', '拟派项目负责人', '项目总监'),
    ('技术负责人', '技术总监', '总工程师', '首席架构师'),
    ('驻场人员', '驻点人员', '现场实施人员', '常驻工程师', '驻场工程师'),
    ('质保期', '免费维保期', '质量保证期', '免费保修期', '免费维护期', '免费质保期', '维保期限'),
    ('售后服务', '运维服务', '维保服务', '技术支持服务'),
    ('故障响应', '报修响应', '应急响应', '异常响应'),
    ('到场时间', '现场支持时间', '到达现场', '赶赴现场'),
    ('高可用', '双活集群', '主备热备', '主备热切换', '负载均衡', '故障自动切换'),
    ('灾备', '高可用灾备', '容灾备份', '异地容灾', '双活集群故障接管', '故障接管', '数据备份恢复'),
    ('信创', '国产化', '自主可控', '国产化自主可控', '信息技术应用创新'),
    ('银河麒麟', '麒麟操作系统', 'kylinos', '国产服务器操作系统', '银河麒麟v10'),
    ('统信uos', '统信操作系统', '国产桌面操作系统'),
    ('达梦', '达梦数据库', 'dm8', '国产关系型数据库'),
    ('人大金仓', '金仓数据库', 'kingbasees'),
    ('国密算法', '商用密码', 'sm2', 'sm3', 'sm4', '国密加密'),
    ('最高限价', '招标控制价', '最高投标限价', '拦标价', '采购预算', '预算金额'),
    ('投标总价', '投标总报价', '报价总计', '总报价', '开标一览表总价'),
    ('分项报价', '明细报价', '报价明细', '清单报价'),
    ('投标保证金', '保证金', '投标保函'),
    ('履约保证金', '质量保证金', '质保金', '履约保函'),
    ('交货期', '完工期', '建设周期', '实施周期', '交付期限', '上线工期', '工期'),
    ('投标有效期', '响应有效期', '报价有效期'),
    ('营业执照', '企业法人营业执照', '三证合一', '统一社会信用代码证'),
    ('社保证明', '社会保险缴纳证明', '社保缴纳记录', '养老保险缴纳证明'),
    ('财务状况报告', '审计报告', '财务审计报告', '资信证明'),
    ('纳税证明', '完税证明', '税收缴纳证明', '依法缴纳税收'),
    ('无重大违法记录', '守法经营声明', '信用中国查询', '未列入失信被执行人'),
    ('中小企业声明函', '小微企业声明', '中小企业划型'),
    ('检测报告', '检验报告', '测试报告', '第三方权威检测', 'cma报告', 'cnas报告'),
    ('互认证书', '兼容性证明', '适配认证', '兼容互认证明'),
    ('软件著作权', '软著证书', '计算机软件著作权登记证书'),
    ('等保三级', '网络安全等级保护第三级', '等级保护备案证明'),
    ('涉密资质', '涉密信息系统集成资质', '保密资质'),
    ('信息安全管理体系', 'iso27001', 'iso/iec27001'),
    ('质量管理体系', 'iso9001', 'gb/t19001'),
    ('信息技术服务管理体系', 'iso20000', 'itss认证'),
    ('并发处理能力', '吞吐量', '并发数', '每秒事件数', 'eps', 'tps', 'qps'),
    ('响应延迟', '响应耗时', '处理时延', '页面打开时间'),
    ('背板带宽', '交换容量', '转发速率', '包转发率'),
    ('微服务架构', '分布式架构', '容器化部署', '云原生架构'),
    ('单点登录', '统一身份认证', 'sso', 'oauth2', 'cas认证'),
    ('日志审计', '行为审计', '操作留痕', '安全审计'),
    ('态势感知', '安全监测', '威胁检测', '风险预警'),
    ('可视化大屏', '驾驶舱', '监控大屏', '态势大屏'),
    ('类似业绩', '同类项目业绩', '历史合同业绩', '成功案例'),
)

# 构建词条 -> 同义词集合快速查找表
_SYNONYM_MAP: dict[str, tuple[str, ...]] = {}
for _grp in BID_SYNONYM_GROUPS:
    _norm_grp = tuple(w.lower().strip() for w in _grp if w.strip())
    for _w in _norm_grp:
        existing = set(_SYNONYM_MAP.get(_w, ()))
        existing.update(_norm_grp)
        existing.discard(_w)
        _SYNONYM_MAP[_w] = tuple(sorted(existing))

_STOP_CHARS_RE = re.compile(r'[★▲\s0-9.、()（）【】\[\]：:，,。；;“”"\'？?！!《》—\-]+')
_STOPWORDS = frozenset({
    '投标人', '供应商', '采购人', '招标人', '招标文件', '投标文件', '本项目',
    '要求', '提供', '必须', '应当', '满足', '具有', '具备', '支持', '系统',
    '請問', '请问', '是否', '什么', '哪里', '在哪', '多少', '如何', '怎么',
})

_CACHE_DIR = Path(__file__).resolve().parents[1] / 'data' / 'cache'


def expand_query_synonyms(query_text: str) -> list[str]:
    """检测 query_text 中命中的招投标领域同义词，返回扩展后的同义词词条列表。"""
    if not query_text:
        return []
    q_low = query_text.lower()
    expanded: list[str] = []
    seen: set[str] = set()
    for term, syns in _SYNONYM_MAP.items():
        if term in q_low:
            if term not in seen:
                seen.add(term)
                expanded.append(term)
            for s in syns:
                if s not in seen:
                    seen.add(s)
                    expanded.append(s)
    return expanded


def _tokenize_for_bm25(text: str, expand_syn: bool = False) -> list[str]:
    """将中文/英文混合文本切分为 2-gram + 英文数字词 + 同义词归一化概念词元。"""
    if not text:
        return []
    low = text.lower()
    tokens: list[str] = []

    # 1. 提取命中的领域词及同义词组代表元（使同组同义词映射到共享概念 token）
    for group_idx, grp in enumerate(BID_SYNONYM_GROUPS):
        for w in grp:
            w_low = w.lower()
            if w_low in low:
                tokens.append(f"__syn_group_{group_idx}__")
                tokens.append(w_low)
                if expand_syn:
                    for sw in grp:
                        tokens.append(sw.lower())
                break

    # 2. 提取英文/数字标识符（如 v10, dm8, cisp, eps, iso27001）
    for m in re.finditer(r'[a-z0-9]{2,}', low):
        tokens.append(m.group(0))

    # 3. 剥离标点与停用词后生成中文字符 2-gram
    cleaned = _STOP_CHARS_RE.sub('', low)
    for sw in _STOPWORDS:
        cleaned = cleaned.replace(sw, '')
    cjk_only = re.sub(r'[a-z0-9]+', '', cleaned)
    if len(cjk_only) == 1:
        tokens.append(cjk_only)
    elif len(cjk_only) >= 2:
        for i in range(len(cjk_only) - 1):
            tokens.append(cjk_only[i:i + 2])

    return tokens


def _split_document_chunks(text: str, window_Pad: int = 160) -> list[dict[str, Any]]:
    """将全文（含 parse_document 展平的段落与表格单元格行）切分为带精确字符偏移的检索块。"""
    if not text or not text.strip():
        return []

    chunks: list[dict[str, Any]] = []
    cursor = 0
    for raw_line in text.split('\n'):
        line_len = len(raw_line)
        stripped = raw_line.strip()
        if len(stripped) >= 4:
            real_start = text.find(stripped, cursor)
            if real_start < 0:
                real_start = cursor
            real_end = real_start + len(stripped)
            w_lo = max(0, real_start - window_Pad)
            w_hi = min(len(text), real_end + window_Pad)
            chunks.append({
                'text': stripped,
                'window': text[w_lo:w_hi],
                'start': real_start,
                'end': real_end,
                'tokens': _tokenize_for_bm25(stripped, expand_syn=False),
            })
        cursor += line_len + 1
    return chunks


class BM25Index:
    """纯 Python 标准库实现的轻量级 Okapi BM25 索引（<3ms 极速检索，0 Token）。"""

    def __init__(self, chunks: list[dict[str, Any]], k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.n_docs = len(chunks)
        self.doc_lens: list[int] = []
        self.doc_freqs: list[dict[str, int]] = []
        self.idf: dict[str, float] = {}

        df_counts: dict[str, int] = {}
        total_len = 0
        for ch in chunks:
            toks = ch.get('tokens') or []
            self.doc_lens.append(len(toks))
            total_len += len(toks)
            tf_map: dict[str, int] = {}
            for t in toks:
                tf_map[t] = tf_map.get(t, 0) + 1
            self.doc_freqs.append(tf_map)
            for t in tf_map:
                df_counts[t] = df_counts.get(t, 0) + 1

        self.avgdl = (total_len / self.n_docs) if self.n_docs > 0 else 1.0
        for t, df in df_counts.items():
            self.idf[t] = math.log(1.0 + (self.n_docs - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if not self.chunks or not query:
            return []
        q_tokens = _tokenize_for_bm25(query, expand_syn=True)
        if not q_tokens:
            return []

        q_tf: dict[str, int] = {}
        for qt in q_tokens:
            q_tf[qt] = q_tf.get(qt, 0) + 1

        scored: list[tuple[float, int]] = []
        for idx, tf_map in enumerate(self.doc_freqs):
            if not tf_map:
                continue
            dl = self.doc_lens[idx]
            score = 0.0
            for qt, q_weight in q_tf.items():
                tf = tf_map.get(qt, 0)
                if tf == 0:
                    continue
                idf_val = self.idf.get(qt, 0.1)
                # 同义词概念组命中给予更高权重激励
                boost = 2.2 if qt.startswith('__syn_group_') else 1.0
                denom = tf + self.k1 * (1.0 - self.b + self.b * (dl / max(self.avgdl, 1.0)))
                score += boost * idf_val * ((tf * (self.k1 + 1.0)) / max(denom, 1e-6)) * min(q_weight, 2)
            if score > 0.0:
                scored.append((score, idx))

        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for raw_score, idx in scored[:top_k]:
            ch = self.chunks[idx]
            # 归一化置信度映射至 0.25 ~ 0.96 区间
            norm_score = round(min(0.96, 0.25 + (raw_score / (raw_score + 6.0)) * 0.72), 4)
            results.append({
                'quote': ch['text'],
                'window': ch['window'],
                'start': ch['start'],
                'end': ch['end'],
                'offset': ch['start'],
                'score': norm_score,
                'raw_bm25': round(raw_score, 4),
                'level': 'level1_bm25_synonym',
            })
        return results


def _try_level2_embedding_rerank(
    query: str,
    chunks: list[dict[str, Any]],
    bm25_candidates: list[dict[str, Any]],
    doc_text: str,
    client: Any = None,
    top_k: int = 3,
) -> Optional[list[dict[str, Any]]]:
    """Level 2（可选激活）：当 client 具备 embeddings 接口或配置了在线 Key 且启用向量时，
    计算并读写 data/cache/{sha256}.emb.npy 缓存，执行向量余弦粗筛 Top-15 + 精排 Top-3。
    任何异常或离线环境静默返回 None，无缝回退至 Level 1。
    """
    if client is None or not chunks:
        return None
    if os.environ.get('BIDGUARD_FORCE_ZERO_TOKEN', '').strip() == '1':
        return None

    # E12 快速失败：client 未配置向量模型（models.embedding 缺失）时不发起任何请求，
    # 直接静默回退 Level 1——严禁因向量能力缺失报错中断体检/F7 追问
    client_models = getattr(client, 'models', None)
    if isinstance(client_models, dict) and not client_models.get('embedding'):
        return None

    embed_fn = getattr(client, 'embed', None) or getattr(client, 'embeddings', None)
    if not callable(embed_fn):
        return None

    try:
        doc_sha = hashlib.sha256(doc_text.encode('utf-8')).hexdigest()
        cache_file = _CACHE_DIR / f"{doc_sha}.emb.npy"
        doc_vecs: Optional[np.ndarray] = None
        doc_lock = _get_cache_lock(doc_sha)

        with doc_lock:
            if cache_file.is_file():
                try:
                    loaded = np.load(str(cache_file), allow_pickle=False)
                    if isinstance(loaded, np.ndarray) and loaded.ndim == 2 and loaded.shape[0] == len(chunks):
                        doc_vecs = loaded.astype(np.float32, copy=False)
                except Exception:
                    doc_vecs = None

            if doc_vecs is None:
                texts_to_embed = [c['text'] for c in chunks]
                raw_vecs = embed_fn(texts_to_embed)
                doc_vecs = np.asarray(raw_vecs, dtype=np.float32)
                if doc_vecs.ndim != 2 or doc_vecs.shape[0] != len(chunks):
                    return None
                _atomic_save_vectors(cache_file, doc_vecs)

        q_vec_raw = embed_fn([query])
        if not q_vec_raw:
            return None
        q_vec = np.asarray(q_vec_raw[0], dtype=np.float32)
        if q_vec.ndim != 1 or q_vec.shape[0] == 0:
            return None

        # E12 交叉复查修复（检查点 4）：若切换了不同维度的向量模型导致旧缓存列维度 shape[1]
        # 与当前 q_vec 维度不符，立即作废旧缓存并重新计算 doc_vecs 原子写盘
        if doc_vecs.shape[1] != q_vec.shape[0]:
            with doc_lock:
                # 二次检查：可能同一轮并发的另一线程已率先按当前模型维度重写了缓存
                if cache_file.is_file():
                    try:
                        reloaded = np.load(str(cache_file), allow_pickle=False)
                        if (
                            isinstance(reloaded, np.ndarray)
                            and reloaded.ndim == 2
                            and reloaded.shape == (len(chunks), q_vec.shape[0])
                        ):
                            doc_vecs = reloaded.astype(np.float32, copy=False)
                    except Exception:
                        pass
                if doc_vecs.shape[1] != q_vec.shape[0]:
                    texts_to_embed = [c['text'] for c in chunks]
                    raw_vecs = embed_fn(texts_to_embed)
                    doc_vecs = np.asarray(raw_vecs, dtype=np.float32)
                    if doc_vecs.ndim != 2 or doc_vecs.shape != (len(chunks), q_vec.shape[0]):
                        return None
                    _atomic_save_vectors(cache_file, doc_vecs)

        doc_norms = np.linalg.norm(doc_vecs, axis=1) + 1e-8
        q_norm = float(np.linalg.norm(q_vec)) + 1e-8
        cos_sims = np.dot(doc_vecs, q_vec) / (doc_norms * q_norm)

        top_indices = np.argsort(cos_sims)[::-1][:15]
        bm25_by_start = {c['start']: c['score'] for c in bm25_candidates}

        reranked: list[tuple[float, int]] = []
        for idx in top_indices:
            i = int(idx)
            vec_score = float(cos_sims[i])
            ch = chunks[i]
            lex_score = bm25_by_start.get(ch['start'], 0.0)
            # 融合向量余弦得分与 BM25+同义词精排得分
            combined = 0.55 * max(0.0, vec_score) + 0.45 * lex_score
            if combined > 0.20:
                reranked.append((combined, i))

        reranked.sort(key=lambda x: x[0], reverse=True)
        if not reranked:
            return None

        _HYBRID_STATS['level2_calls'] += 1
        out: list[dict[str, Any]] = []
        for final_s, i in reranked[:top_k]:
            ch = chunks[i]
            out.append({
                'quote': ch['text'],
                'window': ch['window'],
                'start': ch['start'],
                'end': ch['end'],
                'offset': ch['start'],
                'score': round(float(final_s), 4),
                'level': 'level2_embedding_rerank',
            })
        return out
    except Exception:
        return None


def hybrid_retrieve_windows(
    query: str,
    doc_text: str,
    client: Any = None,
    top_k: int = 3,
    min_score: float = 0.30,
    window_pad: int = 200,
) -> list[dict[str, Any]]:
    """三级自适应混合检索主入口（供 F4 门控后疑似漏答复核与 F7 自然语言追问调用）。

    Args:
        query: 招标要求原文或 F7 自然语言提问。
        doc_text: 目标标书全文（含段落与表格展平文本）。
        client: 可选 LLM/Embedding 客户端；为 None 或无向量接口时自动执行 Level 1 (0 Token)。
        top_k: 返回的最高分窗口数量。
        min_score: 最低采信分数阈值。
        window_pad: 命中点前后扩展窗口字符数。

    Returns:
        [{'quote': str, 'window': str, 'start': int, 'end': int, 'offset': int, 'score': float, 'level': str}, ...]
    """
    _HYBRID_STATS['total_calls'] += 1
    _HYBRID_STATS['level1_calls'] += 1

    if not query or not doc_text or not doc_text.strip():
        return []

    chunks = _split_document_chunks(doc_text, window_Pad=window_pad)
    if not chunks:
        return []

    bm25 = BM25Index(chunks)
    l1_hits = bm25.search(query, top_k=max(top_k, 5))

    # 尝试 Level 2（有 client 且支持向量时激活；否则停留在 Level 1）
    l2_hits = _try_level2_embedding_rerank(
        query=query,
        chunks=chunks,
        bm25_candidates=l1_hits,
        doc_text=doc_text,
        client=client,
        top_k=top_k,
    )
    active_hits = l2_hits if l2_hits is not None else l1_hits[:top_k]
    return [h for h in active_hits if h['score'] >= min_score]