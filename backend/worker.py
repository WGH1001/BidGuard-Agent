# -*- coding: utf-8 -*-
"""B2 / E3R / E5 / E11 / E15 · 进程内线程池任务执行器 + SSE 进度广播总线 + 三级 LLM 客户端降级解析器 + 分阶段耗时统计 + 发现页码定位与开标日期自动回填。

严格遵循 INTERFACE.md §1、§3.3、E3R 实锤 B、E5 措辞动态化、E11 耗时可观测性与 E15 页码/开标日期增强：
1. 状态机五态推进：created -> extracting -> checking -> grading -> delivered（异常转 failed）；
2. SSE 三种标准事件：event: progress / event: done / event: error；
3. E3R 实锤 B 修复：同一任务再次触发 run 时，submit_check_task 立即调用 broadcaster.clear(task_id)
   清除上一轮残留的 done/error 终态帧，并将数据库 status 同步重置为 'extracting'、progress=0；
4. 溯源章铁律：F2 算术与 F3 效期/串名计算类发现默认 quote_verified=False（仅当 F2 锚点精确命中原文子串时置 True）；
5. E5 措辞动态化：F4 发现的 category 与 suggestion 按条款符号（★ / ▲ / ★或▲）动态生成；
6. E11 耗时可观测性：记录 started_at / finished_at（UTC）与 duration_json（f1_ms/f2_ms/f3_ms/f4_ms/grading_ms/total_ms），
   重跑时自动清空旧耗时，异常失败路径亦保留已完成阶段耗时以便定位卡死点；
7. E15 页码定位与开标日期回填：复用 response_checker 统一双轨页码上下文为 F2/F3/自定义红线赋 page，
   F2 采用"名称+金额同现 -> 报价章节 ±3000 字窗口 -> 诚实回退 None"三级防重名锚定链；开标日期未填时从招标书 0-Token 正则提取回填。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from backend.database import PROJECT_ROOT, get_session_factory
from backend.models import (
    BidCheckTask,
    BidFinding,
    BidRuleConfig,
    Tenant,
    UserModelConfig,
    ensure_bid_check_tasks_columns,
    ensure_user_model_configs_columns,
)
from backend.utils.crypto import decrypt_api_key
from core.deviation_exporter import export_deviation_table
from core.llm_gateway import (
    build_client_from_user_config,
    get_llm_client,
    is_inner_model_url_allowed,
    is_strict_inner_model_only,
)
from core.parser import parse_document
from core.price_extractor import run_price_check
from core.qualification_scanner import (
    parse_cn_date,
    resolve_effective_social_requirement,
    scan_blind_bid_compliance,
    scan_qualifications,
)
from core.quote_verifier import verify_quote
from core.response_checker import (
    _build_unified_page_context,
    _resolve_unified_page,
    run_response_check,
)
from core.tender_extractor import (
    build_review_checklist,
    extract_blind_bid_requirements,
    extract_open_date_from_tender_text,
    extract_open_date_with_evidence,
)
from core.tender_llm import extract_open_date_fallback_with_llm, extract_tender_with_llm

DEFAULT_FALLBACK_OPEN_DATE = '2026-11-05'
_DEFAULT_F1_HEARTBEAT_SEC = 5.0


class _PipelineCancelled(BaseException):
    """协作式取消异常：继承 BaseException 以穿透内部降级 except Exception 块，杜绝静默降级产出半残报告。"""


PipelineCancelled = _PipelineCancelled


# E15 §2.3：报价相关章节锚点词（用于定位投标书中的报价表区域中心偏移）
_PRICE_SECTION_HEADING_RE = re.compile(
    r'(?:开\s*标\s*一\s*览\s*表|报\s*价\s*一\s*览\s*表|分\s*项\s*报\s*价\s*表|分\s*项\s*报\s*价\s*明\s*细|投\s*标\s*报\s*价\s*明\s*细|报\s*价\s*汇\s*总\s*表)'
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def resolve_project_path(rel_or_abs: Optional[str | Path]) -> Path:
    """将数据库存储的相对路径还原为基于 PROJECT_ROOT 的绝对路径。"""
    if not rel_or_abs:
        return Path('')
    p = Path(str(rel_or_abs))
    if p.is_absolute():
        return p
    return (PROJECT_ROOT / p).resolve()


def _format_amount_variants(amt_str: Optional[str]) -> list[str]:
    """生成金额字符串的无逗号与千分位逗号两种字面量变体（如 '84500.00' 与 '84,500.00'、'84500'）。"""
    if not amt_str:
        return []
    raw = str(amt_str).strip()
    if not raw:
        return []
    variants: list[str] = []
    clean = raw.replace(',', '')
    try:
        from decimal import Decimal
        d = Decimal(clean)
        q2 = f"{d:,.2f}"
        q2_plain = f"{d:.2f}"
        for cand in (raw, q2, q2_plain, str(d.normalize())):
            if cand and cand not in variants:
                variants.append(cand)
    except Exception:
        variants.append(raw)
    return variants


def _find_price_section_centers(bid_text: str, f2_res: dict[str, Any]) -> list[int]:
    """E15 §2.3b：收集投标书中报价区域（开标一览表/分项报价表/已提取分项特征串）的中心偏移列表。"""
    centers: list[int] = []
    if not bid_text:
        return centers

    explicit_off = f2_res.get('price_section_offset')
    if isinstance(explicit_off, int) and 0 <= explicit_off < len(bid_text):
        centers.append(explicit_off)

    for m in _PRICE_SECTION_HEADING_RE.finditer(bid_text):
        centers.append(m.start())

    # 利用已提取的其他分项"名称+金额"共现位置作为报价表区域锚点
    for it in (f2_res.get('items') or [])[:10]:
        nm = str(it.get('name') or '').strip()
        if not nm:
            continue
        for amt_v in _format_amount_variants(it.get('total_price')) + _format_amount_variants(it.get('unit_price')):
            for m_nm in re.finditer(re.escape(nm), bid_text):
                pos = m_nm.start()
                win_hi = min(len(bid_text), pos + len(nm) + 120)
                if amt_v in bid_text[pos:win_hi]:
                    centers.append(pos)
                    break
    return centers


def locate_f2_finding_in_bid_text(
    f2_item: dict[str, Any],
    f2_res: dict[str, Any],
    bid_text: str,
) -> tuple[Optional[str], Optional[int]]:
    """E15 §2.3：对 F2 finding 执行防前文重名污染的三级降级锚定链，返回 (quote, quote_offset)。

    降级匹配链：
    a. 首选：分项名称 + 该行金额字面量在同行/邻近窗口（120字符内）共现，精确锁死报价表行；
    b. 次选：在报价相关章节区域（以开标一览表/分项报价表 offset 为中心 ±3000 字符窗口）内搜分项名称或 anchor_text；
    c. 末选：若为表级/全局金额发现（如 TOTAL_MISMATCH / AMBIGUOUS_BID_TOTAL），在报价区域或全文搜金额特征串；
       仍搜不到（如被合并单元格拆散）则诚实返回 (None, None)，严禁硬造或错锚到前文技术章节。
    """
    if not bid_text:
        return None, None

    item_name = str(f2_item.get('name') or f2_item.get('anchor_name') or '').strip()
    anchor_text = str(f2_item.get('anchor_text') or '').strip()

    # 收集该 finding 关联的金额候选串
    amt_candidates: list[str] = []
    for k in ('actual', 'expected', 'bid_total', 'table_total', 'unit_price', 'total_price'):
        val = f2_item.get(k)
        if val is not None:
            for v in _format_amount_variants(str(val)):
                if v not in amt_candidates:
                    amt_candidates.append(v)

    if item_name:
        for it in f2_res.get('items') or []:
            if str(it.get('name') or '').strip() == item_name:
                for k in ('raw_unit_text', 'raw_total_text', 'unit_price', 'total_price'):
                    for v in _format_amount_variants(it.get(k)):
                        if v not in amt_candidates:
                            amt_candidates.append(v)

    # ── 级别 a（首选）：分项名称 + 金额字面量同现（防前文技术方案重名污染）──
    if item_name and amt_candidates:
        for amt_s in amt_candidates:
            direct_pair = f"{item_name} {amt_s}"
            idx_pair = bid_text.find(direct_pair)
            if idx_pair >= 0:
                return direct_pair, idx_pair

        for m_nm in re.finditer(re.escape(item_name), bid_text):
            pos = m_nm.start()
            win_hi = min(len(bid_text), pos + len(item_name) + 120)
            window_slice = bid_text[pos:win_hi]
            for amt_s in amt_candidates:
                if amt_s and amt_s in window_slice:
                    amt_end = window_slice.find(amt_s) + len(amt_s)
                    matched_quote = window_slice[:amt_end].strip()
                    if matched_quote and matched_quote in bid_text:
                        return matched_quote, pos
                    return item_name, pos

    # ── 级别 b（次选）：在报价相关章节区域（中心 ±3000 字符窗口）内搜分项名称或 anchor_text ──
    centers = _find_price_section_centers(bid_text, f2_res)
    search_targets = [t for t in (anchor_text, item_name) if t]
    if centers and search_targets:
        for target in search_targets:
            if target not in bid_text:
                continue
            for c_off in centers:
                w_lo = max(0, c_off - 3000)
                w_hi = min(len(bid_text), c_off + 3000)
                sub_pos = bid_text.find(target, w_lo, w_hi)
                if sub_pos >= 0:
                    return target, sub_pos

    # ── 级别 c（表级/歧义金额锚点或诚实回退）：对无分项名称的表级发现搜 anchor_text ──
    if not item_name and anchor_text and anchor_text in bid_text:
        if centers:
            for c_off in centers:
                w_lo = max(0, c_off - 3000)
                w_hi = min(len(bid_text), c_off + 3000)
                sub_pos = bid_text.find(anchor_text, w_lo, w_hi)
                if sub_pos >= 0:
                    return anchor_text, sub_pos
        pos_global = bid_text.find(anchor_text)
        if pos_global >= 0:
            return anchor_text, pos_global

    return None, None


def _enrich_detail_with_page_meta(
    detail_dict: Optional[dict[str, Any]],
    phys_page: Optional[int],
    footer_page: Optional[int],
    is_docx_page: bool,
) -> dict[str, Any]:
    """将统一页码上下文解析出的双轨页码元数据写入 detail_json（与 F4 RiskCard 渲染协议完全对齐）。"""
    out = dict(detail_dict or {})
    if phys_page is not None:
        out['bid_page'] = phys_page
        out['bid_footer_page'] = footer_page
        out['bid_page_is_docx'] = is_docx_page
    return out


def resolve_f3_suggestion(f3_item: dict[str, Any]) -> str:
    """E27 §2.1.2：按 F3 发现的 code 给出针对性整改建议，避免一刀切固定文案。"""
    code_str = str(f3_item.get('code') or '').strip()
    if code_str == 'SOCIAL_MONTH_MISSING':
        return '请按本项目开标日期前的社保窗口期要求，补齐对应月份的单位社保缴纳证明或税务完税凭证扫描件'
    if code_str in ('CERT_EXPIRED', 'CERT_EXPIRING'):
        return '请替换处于本项目开标日期有效期内的最新证照/体系认证证书扫描件'
    if code_str in ('FORBIDDEN_NAME', 'WRONG_BUYER_NAME'):
        return '请全局排查并清理投标书中残留的外部项目或历史采购人单位名称，统一为本项目采购人全称'
    if code_str == 'PLACEHOLDER':
        return '请排查并补齐投标书中未填写的空白占位符（如 XXX、待定、空白下划线等）'
    return '请替换有效期内证照或清理外部项目单位名称'


def resolve_f4_category_and_suggestion(f4_item: dict[str, Any]) -> tuple[str, str]:
    """E5 ⑤ & ⑥ + E27 §2.3.2：按 f4_item 的 clause_role、mark 或 clause_no 前缀动态生成 category 与 suggestion。"""
    if f4_item.get('clause_role') == 'submission_rule':
        return (
            '封标递交与形式自检提醒',
            '请于封标打印与递交阶段按招标要求逐项核对正副本份数、电子版介质（PDF+Word）及印章字迹清晰度，无需在标书正文增补偏离说明',
        )
    code_str = str(f4_item.get('code') or 'MISSING_RESPONSE')
    mark_str = str(f4_item.get('mark') or '').strip()
    clause_no_str = str(f4_item.get('clause_no') or '').strip()
    combined_prefix = f"{mark_str}{clause_no_str}"

    if '★' in combined_prefix:
        cat_label = '★条款响应核验'
        sym_label = '★'
    elif '▲' in combined_prefix:
        cat_label = '▲条款响应核验'
        sym_label = '▲'
    else:
        cat_label = '★/▲条款响应核验'
        sym_label = '★或▲'

    if code_str == 'CONTRADICTION' and f4_item.get('scope') == 'bid_internal':
        category = '前后承诺矛盾检测'
    elif code_str == 'CONTRADICTION' and not combined_prefix:
        category = '前后承诺矛盾检测'
    else:
        category = cat_label

    suggestion = f"请在投标书中补齐对该 {sym_label} 条款的明确正面响应与证明材料，并统一前后章节承诺参数"
    return category, suggestion


def resolve_effective_llm_client(session: Session, user_id: Optional[int] = None) -> tuple[Any, str]:
    """按'用户自定义配置 -> 全局 env 配置 -> None（0 Token 确定性降级）'三级获取 LLM 客户端。"""
    if os.environ.get('BIDGUARD_FORCE_ZERO_TOKEN', '').strip() == '1':
        return None, 'skipped'

    strict_inner = is_strict_inner_model_only()

    if user_id is not None:
        try:
            ensure_user_model_configs_columns(session.get_bind())
            cfg = session.query(UserModelConfig).filter_by(user_id=int(user_id)).first()
            if cfg is not None and cfg.base_url and cfg.api_key_encrypted and cfg.model_flash:
                if strict_inner and not is_inner_model_url_allowed(cfg.base_url):
                    logging.getLogger(__name__).warning(
                        '已开启 STRICT_INNER_MODEL_ONLY，忽略用户 %s 的存量外网自定义模型配置并回退内网网关',
                        user_id,
                    )
                else:
                    plain_key = decrypt_api_key(cfg.api_key_encrypted)
                    client = build_client_from_user_config(
                        base_url=cfg.base_url,
                        api_key=plain_key,
                        model_flash=cfg.model_flash,
                        model_flagship=cfg.model_flagship or cfg.model_flash,
                        supports_reasoning_effort=bool(cfg.supports_reasoning_effort),
                        supports_vision=bool(getattr(cfg, 'supports_vision', False)),
                    )
                    return client, 'done'
        except Exception:
            pass

    zhipu_key = os.environ.get('ZHIPU_API_KEY', '').strip()
    priv_key = os.environ.get('PRIVATE_API_KEY', '').strip()
    if strict_inner:
        if priv_key:
            try:
                client = get_llm_client('prod')
                return client, 'done'
            except Exception:
                pass
        return None, 'skipped'

    has_valid_env_key = (
        (zhipu_key and zhipu_key != 'your_api_key_here')
        or bool(priv_key)
    )
    if has_valid_env_key:
        try:
            env_name = os.environ.get('MODEL_ENV', '').strip() or None
            client = get_llm_client(env_name)
            return client, 'done'
        except Exception:
            pass

    return None, 'skipped'


class SSEBroadcaster:
    """线程安全 SSE 进度广播总线（按 INTERFACE.md §3.3 推送 progress / done / error 事件）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[int, set[queue.Queue]] = {}
        self._latest: dict[int, tuple[str, dict[str, Any]]] = {}

    def clear(self, task_id: int) -> None:
        """E3R 实锤 B：清理指定 task_id 上一轮残留的 latest 终态帧，并清空在途订阅队列中的旧帧。"""
        tid = int(task_id)
        with self._lock:
            self._latest.pop(tid, None)
            subs = list(self._subscribers.get(tid, ()))
        for q in subs:
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def subscribe(self, task_id: int) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=128)
        tid = int(task_id)
        with self._lock:
            self._subscribers.setdefault(tid, set()).add(q)
        return q

    def unsubscribe(self, task_id: int, q: queue.Queue) -> None:
        tid = int(task_id)
        with self._lock:
            subs = self._subscribers.get(tid)
            if subs:
                subs.discard(q)
                if not subs:
                    self._subscribers.pop(tid, None)

    def publish(self, task_id: int, event_name: str, data: dict[str, Any]) -> None:
        tid = int(task_id)
        frame = (event_name, dict(data))
        with self._lock:
            self._latest[tid] = frame
            subs = list(self._subscribers.get(tid, ()))
        for q in subs:
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass

    def latest(self, task_id: int) -> Optional[tuple[str, dict[str, Any]]]:
        with self._lock:
            return self._latest.get(int(task_id))

_logger = logging.getLogger(__name__)
_DEVIATION_OUT_DIR = (PROJECT_ROOT / 'output' / 'deviation').resolve()


def deviation_stats_sidecar_path(task_id: int) -> Path:
    """偏离表统计快照（sidecar JSON）：产物 docx 旁，供 GET 复用秒回。"""
    return _DEVIATION_OUT_DIR / f"{int(task_id)}_stats.json"


def build_and_cache_deviation_artifacts(
    *,
    task_id: int,
    tender_abs: Path,
    bid_abs: Path,
    requirements: list[dict[str, Any]],
    client=None,
    f4_result: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """生成偏离表 docx 并原子写入统计快照 sidecar JSON（复用 f4_result 可零额外 LLM 预生成）。"""
    _DEVIATION_OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_filename = f"{int(task_id)}_偏离表.docx"
    out_abs = (_DEVIATION_OUT_DIR / out_filename).resolve()

    res = export_deviation_table(
        tender_path=tender_abs,
        bid_path=bid_abs,
        output_path=out_abs,
        requirements=requirements,
        client=client,
        f4_result=f4_result,
    )
    payload = {
        'task_id': int(task_id),
        'download_url': f"/api/files/{out_abs.relative_to(PROJECT_ROOT.resolve()).as_posix()}",
        'output_filename': out_filename,
        'total_rows': int(res.get('total_rows', 0)),
        'no_deviation_count': int(res.get('no_deviation_count', 0)),
        'negative_deviation_count': int(res.get('negative_deviation_count', 0)),
        'missing_count': int(res.get('missing_count', 0)),
        'contradiction_count': int(res.get('contradiction_count', 0)),
        'rows': list(res.get('rows') or []),
    }
    sidecar = deviation_stats_sidecar_path(task_id)
    tmp_sidecar = sidecar.with_suffix('.json.tmp')
    tmp_sidecar.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    os.replace(str(tmp_sidecar), str(sidecar))
    return payload


def _cleanup_task_deviation_artifacts(task_id: int) -> None:
    """E25 §2.1.3：清理指定任务旧的偏离表 docx 与统计快照 sidecar，防止任务重跑后 F5 仍返回过时快照。"""
    for fname in (f"{int(task_id)}_偏离表.docx", f"{int(task_id)}_stats.json"):
        try:
            (_DEVIATION_OUT_DIR / fname).unlink(missing_ok=True)
        except OSError:
            pass

class BidTaskWorker:
    """后台线程池核验流水线执行器。"""

    def __init__(self, session_factory=None, max_workers: int = 4) -> None:
        self.session_factory = session_factory or get_session_factory()
        self.broadcaster = SSEBroadcaster()
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='bidguard_worker')
        self._lock = threading.Condition(threading.Lock())
        self._in_flight: set[int | str] = set()
        self._cancel_flags: dict[int | str, threading.Event] = {}
        self._f1_heartbeat_interval_sec: float = _DEFAULT_F1_HEARTBEAT_SEC
        self._shutdown = False

    @staticmethod
    def _norm_job_key(task_id: int | str) -> int | str:
        return task_id if isinstance(task_id, str) else int(task_id)

    def register_task_execution(self, task_id: int | str) -> threading.Event:
        """为指定任务（整数 task_id 或字符串 job_key 如 composer:1）注册取消信号并标记在途。"""
        tid = self._norm_job_key(task_id)
        with self._lock:
            ev = threading.Event()
            self._cancel_flags[tid] = ev
            self._in_flight.add(tid)
            return ev

    def unregister_task_execution(self, task_id: int | str, ev: Optional[threading.Event] = None) -> None:
        """任务退出时清理本次运行态（比对 Event 实例防旧线程误删新一轮重跑令牌）。"""
        tid = self._norm_job_key(task_id)
        with self._lock:
            cur_ev = self._cancel_flags.get(tid)
            if ev is None or cur_ev is ev or cur_ev is None:
                self._cancel_flags.pop(tid, None)
                self._in_flight.discard(tid)
                self._lock.notify_all()

    def request_cancel(self, task_id: int | str) -> bool:
        """向在途任务发出协作式中断请求，返回是否命中活跃或注册中任务。"""
        tid = self._norm_job_key(task_id)
        with self._lock:
            ev = self._cancel_flags.get(tid)
            if ev is not None:
                ev.set()
                return True
            if tid in self._in_flight:
                ev = threading.Event()
                ev.set()
                self._cancel_flags[tid] = ev
                return True
            return False

    def is_cancel_requested(self, task_id: int | str) -> bool:
        tid = self._norm_job_key(task_id)
        with self._lock:
            ev = self._cancel_flags.get(tid)
            return bool(ev is not None and ev.is_set())

    def check_cancelled(self, task_id: int | str, ev: Optional[threading.Event] = None) -> None:
        """协作式取消检查点：命中即抛出 _PipelineCancelled(BaseException)。"""
        if ev is not None and ev.is_set():
            raise _PipelineCancelled('用户中断')
        if self.is_cancel_requested(task_id):
            raise _PipelineCancelled('用户中断')

    def submit_background_job(
        self,
        job_key: str,
        fn: Callable[[threading.Event], None],
        *,
        sync: bool = False,
    ) -> None:
        """提交通用后台任务（供 E38 标书编制与多标书比对异步执行、取消与优雅关闭回收）。"""
        cancel_ev = self.register_task_execution(job_key)

        def _runner() -> None:
            try:
                fn(cancel_ev)
            finally:
                self.unregister_task_execution(job_key, cancel_ev)

        if sync:
            _runner()
            return
        with self._lock:
            if self._shutdown:
                self.unregister_task_execution(job_key, cancel_ev)
                raise RuntimeError('Worker 已关闭')
        self._executor.submit(_runner)

    def _reset_task_for_rerun(self, task_id: int) -> None:
        """E3R 实锤 B + E11 + E25 + E30：启动/重跑前清 SSE 终态缓存、清旧偏离表、置空旧 stats_json 并置 extracting(progress=0)。"""
        self.broadcaster.clear(task_id)
        _cleanup_task_deviation_artifacts(task_id)
        reset_utc = _utc_now()
        session: Session = self.session_factory()
        try:
            ensure_bid_check_tasks_columns(session.get_bind())
            task = session.query(BidCheckTask).filter_by(id=task_id).first()
            if task is not None:
                task.status = 'extracting'
                task.progress = 0
                task.stage_detail = '正在启动新一轮防废标核验流水线…'
                task.error_msg = None
                task.stats_json = None
                task.started_at = None
                task.finished_at = None
                task.duration_json = None
                task.updated_at = reset_utc
                if isinstance(task.config_json, dict) and task.config_json.get('cancelled_by_user'):
                    cfg_copy = dict(task.config_json)
                    cfg_copy.pop('cancelled_by_user', None)
                    task.config_json = cfg_copy
                session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

        self.broadcaster.publish(
            task_id,
            'progress',
            {
                'task_id': task_id,
                'status': 'extracting',
                'progress': 0,
                'stage_detail': '正在启动新一轮防废标核验流水线…',
                'updated_at': reset_utc.isoformat(),
            },
        )

    def submit_check_task(self, task_id: int, sync: bool = False) -> None:
        tid = int(task_id)
        if sync:
            ev = self.register_task_execution(tid)
            self._reset_task_for_rerun(tid)
            self._run_pipeline(tid, cancel_ev=ev)
            return
        with self._lock:
            if self._shutdown or (tid in self._in_flight and not self.is_cancel_requested(tid)):
                return
            ev = threading.Event()
            self._cancel_flags[tid] = ev
            self._in_flight.add(tid)

        self._reset_task_for_rerun(tid)

        try:
            self._executor.submit(self._run_pipeline, tid, ev)
        except Exception:
            self.unregister_task_execution(tid, ev)
            raise

    def _emit_stage(self, session: Session, task: BidCheckTask, status: str, progress: int, detail: str) -> None:
        now_utc = _utc_now()
        task.status = status
        task.progress = int(progress)
        task.stage_detail = detail
        task.updated_at = now_utc
        session.commit()
        frame_data: dict[str, Any] = {
            'task_id': task.id,
            'status': status,
            'progress': int(progress),
            'stage_detail': detail,
            'updated_at': now_utc.isoformat(),
        }
        if task.started_at is not None:
            frame_data['started_at'] = task.started_at.isoformat()
        self.broadcaster.publish(task.id, 'progress', frame_data)

    def _run_pipeline(self, task_id: int, cancel_ev: Optional[threading.Event] = None) -> None:
        if cancel_ev is None:
            cancel_ev = self.register_task_execution(task_id)
        session: Session = self.session_factory()
        pipeline_t0 = time.perf_counter()
        duration_record: dict[str, Optional[int]] = {
            'f1_ms': None,
            'f_parse_ms': None,
            'f2_ms': None,
            'f3_ms': None,
            'f4_ms': None,
            'grading_ms': None,
            'total_ms': None,
        }
        try:
            self.check_cancelled(task_id, cancel_ev)
            ensure_bid_check_tasks_columns(session.get_bind())
            task = session.query(BidCheckTask).filter_by(id=task_id).first()
            if task is None:
                return

            # E11：流水线开头写入 started_at（UTC）
            started_utc = _utc_now()
            task.started_at = started_utc
            task.finished_at = None
            task.duration_json = None
            task.updated_at = started_utc
            session.commit()

            tender_abs = resolve_project_path(task.tender_path)
            bid_abs = resolve_project_path(task.bid_path)
            if not tender_abs.is_file() or not bid_abs.is_file():
                raise FileNotFoundError("招标文件或投标文件不存在，无法执行体检流水线")

            client, llm_status = resolve_effective_llm_client(session, task.user_id)

            cfg_snap = dict(task.config_json or {})
            rule_cfg = (
                session.query(BidRuleConfig)
                .filter_by(tenant_id=task.tenant_id, is_default=True)
                .first()
            )
            existing_open_src = str(cfg_snap.get('open_date_source') or '').strip()
            manual_open_date = (
                str(cfg_snap.get('open_date') or '').strip()
                if existing_open_src in ('', 'manual')
                else ''
            )
            rule_open_date = str(
                rule_cfg.open_date.isoformat() if (rule_cfg and rule_cfg.open_date) else ''
            ).strip()
            open_date_str = manual_open_date or rule_open_date or DEFAULT_FALLBACK_OPEN_DATE
            open_date_fallback_used = False

            social_months = list(
                cfg_snap.get('required_social_months')
                or (rule_cfg.required_social_months_json if rule_cfg else None)
                or []
            )
            buyer_name = str(
                cfg_snap.get('buyer_name')
                or (rule_cfg.buyer_name if rule_cfg else '')
                or ''
            )
            forbidden_names = list(cfg_snap.get('forbidden_names') or [])
            custom_redlines = list(
                cfg_snap.get('custom_redlines')
                or (rule_cfg.custom_redlines_json if rule_cfg else None)
                or []
            )

            # ── 阶段 1：extracting（F1 招标红线与 ★/▲ 条款提取 + E15/E23 开标日期自动识别回填）──
            self.check_cancelled(task_id, cancel_ev)
            self._emit_stage(session, task, 'extracting', 20, 'F1 正在提取招标文件废标红线与 ★/▲ 实质性条款')
            t_f1 = time.perf_counter()
            last_f1_emit = [0.0]

            def _on_f1_clause_progress(done_cnt: int, total_cnt: int) -> None:
                self.check_cancelled(task_id, cancel_ev)
                if total_cnt <= 0 or done_cnt <= 0:
                    return
                now_ts = time.perf_counter()
                if (
                    done_cnt < total_cnt
                    and last_f1_emit[0] > 0.0
                    and (now_ts - last_f1_emit[0]) < self._f1_heartbeat_interval_sec
                ):
                    return
                last_f1_emit[0] = now_ts
                self._emit_stage(
                    session,
                    task,
                    'extracting',
                    20,
                    f"F1 正在逐条判定废标红线与实质性条款（已判定 {done_cnt} / {total_cnt} 条）",
                )

            if client is not None:
                try:
                    f1_res = extract_tender_with_llm(
                        str(tender_abs),
                        client=client,
                        progress_cb=_on_f1_clause_progress,
                    )
                except _PipelineCancelled:
                    raise
                except Exception:
                    self.check_cancelled(task_id, cancel_ev)
                    f1_res = build_review_checklist(str(tender_abs))
                    llm_status = 'skipped'
            else:
                f1_res = build_review_checklist(str(tender_abs))

            self.check_cancelled(task_id, cancel_ev)

            marked_clauses = list(f1_res.get('marked_clauses') or [])
            invalid_bid_clauses = list(f1_res.get('invalid_bid_clauses') or [])
            f1_requirements = marked_clauses + invalid_bid_clauses

            # E23 §2.1.2：优先级为 任务手填 > 标书原文自动提取 > 正则未命中时 AI 局部窗兜底 > 规则页默认日期 > DEFAULT_FALLBACK_OPEN_DATE
            # 同时写入 open_date_source / open_date_quote 显式来源可观测性，并在手填与标书原文不一致时追加 OPEN_DATE_MISMATCH 预警
            tender_parsed = parse_document(str(tender_abs))
            blind_bid_info = f1_res.get('blind_bid_info') if isinstance(f1_res, dict) else None
            if not isinstance(blind_bid_info, dict):
                blind_bid_info = extract_blind_bid_requirements(
                    tender_parsed.text,
                    page_ctx=_build_unified_page_context(str(tender_abs)),
                )
            tender_date_ev = extract_open_date_with_evidence(tender_parsed.text)
            detected_open_date = ''
            detected_source = ''
            detected_quote = ''
            detected_offset: Optional[int] = None
            if tender_date_ev and tender_date_ev.get('open_date'):
                detected_open_date = str(tender_date_ev['open_date']).strip()
                detected_source = 'tender_extracted'
                detected_quote = str(tender_date_ev.get('quote') or '')
                detected_offset = tender_date_ev.get('offset')
            elif f1_res.get('open_date'):
                detected_open_date = str(f1_res['open_date']).strip()
                detected_source = str(f1_res.get('open_date_source') or 'tender_extracted')
                detected_quote = str(f1_res.get('open_date_quote') or '')
            elif client is not None and tender_parsed.text:
                llm_date_ev = extract_open_date_fallback_with_llm(tender_parsed.text, client)
                if llm_date_ev and llm_date_ev.get('open_date'):
                    detected_open_date = str(llm_date_ev['open_date']).strip()
                    detected_source = 'tender_llm'
                    detected_quote = str(llm_date_ev.get('quote') or '')
                    detected_offset = llm_date_ev.get('offset')

            open_date_mismatch: Optional[dict[str, Any]] = None

            if not manual_open_date:
                if detected_open_date:
                    open_date_str = detected_open_date
                    cfg_snap['open_date'] = detected_open_date
                    cfg_snap['open_date_source'] = detected_source
                    if detected_quote:
                        cfg_snap['open_date_quote'] = detected_quote[:240]
                    else:
                        cfg_snap.pop('open_date_quote', None)
                    cfg_snap.pop('open_date_effective', None)
                    cfg_snap.pop('open_date_detected', None)
                    cfg_snap.pop('open_date_detected_source', None)
                    task.config_json = dict(cfg_snap)
                    task.updated_at = _utc_now()
                    session.commit()
                    src_label = 'AI 局部窗提取' if detected_source == 'tender_llm' else '标书正文提取'
                    self._emit_stage(
                        session,
                        task,
                        'extracting',
                        35,
                        f"已自动识别开标日期（{src_label}）：{detected_open_date}",
                    )
                elif rule_open_date:
                    open_date_str = rule_open_date
                    cfg_snap.pop('open_date', None)
                    cfg_snap.pop('open_date_quote', None)
                    cfg_snap.pop('open_date_detected', None)
                    cfg_snap.pop('open_date_detected_source', None)
                    cfg_snap['open_date_source'] = 'rule_default'
                    cfg_snap['open_date_effective'] = rule_open_date
                    task.config_json = dict(cfg_snap)
                    task.updated_at = _utc_now()
                    session.commit()
                else:
                    open_date_str = DEFAULT_FALLBACK_OPEN_DATE
                    open_date_fallback_used = True
                    cfg_snap.pop('open_date', None)
                    cfg_snap.pop('open_date_quote', None)
                    cfg_snap.pop('open_date_detected', None)
                    cfg_snap.pop('open_date_detected_source', None)
                    cfg_snap['open_date_source'] = 'fallback_default'
                    cfg_snap['open_date_effective'] = DEFAULT_FALLBACK_OPEN_DATE
                    task.config_json = dict(cfg_snap)
                    task.updated_at = _utc_now()
                    session.commit()
            else:
                cfg_snap['open_date_source'] = 'manual'
                cfg_snap.pop('open_date_effective', None)
                manual_d = parse_cn_date(manual_open_date)
                if (
                    manual_d is not None
                    and detected_open_date
                    and manual_d.strftime('%Y-%m-%d') != detected_open_date
                ):
                    open_date_mismatch = {
                        'manual_open_date': manual_d.strftime('%Y-%m-%d'),
                        'detected_open_date': detected_open_date,
                        'detected_source': detected_source,
                        'quote': detected_quote,
                        'offset': detected_offset,
                    }
                    cfg_snap['open_date_detected'] = detected_open_date
                    cfg_snap['open_date_detected_source'] = detected_source
                    if detected_quote:
                        cfg_snap['open_date_quote'] = detected_quote[:240]
                else:
                    cfg_snap.pop('open_date_detected', None)
                    cfg_snap.pop('open_date_detected_source', None)
                    if detected_quote:
                        cfg_snap['open_date_quote'] = detected_quote[:240]
                task.config_json = dict(cfg_snap)
                task.updated_at = _utc_now()
                session.commit()

            duration_record['f1_ms'] = max(0, int(round((time.perf_counter() - t_f1) * 1000)))

            # ── 阶段 1.5：投标书全文与内嵌扫描图预解析（冷 OCR 耗时独立归属 f_parse_ms，F2/F3/F4 共享缓存）──
            self.check_cancelled(task_id, cancel_ev)
            self._emit_stage(
                session,
                task,
                'extracting',
                42,
                '正在解析投标文件全文与内嵌扫描图片（首次冷 OCR 耗时较长，完成后自动写入磁盘缓存）',
            )
            t_parse = time.perf_counter()
            last_ocr_emit = [0.0]

            is_bid_pdf = str(bid_abs).lower().endswith('.pdf')
            ocr_unit = '页' if is_bid_pdf else '张'
            flush_step = 5 if is_bid_pdf else 10

            def _on_bid_ocr_progress(done_items: int, total_items: int, _cached_hits: int) -> None:
                self.check_cancelled(task_id, cancel_ev)
                if total_items <= 0:
                    if _cached_hits > 0:
                        self._emit_stage(
                            session,
                            task,
                            'extracting',
                            50,
                            f"Stage 1.5 扫描件/内嵌图片 OCR 识别：已从本地缓存秒级加载 {_cached_hits} {ocr_unit}历史识别结果",
                        )
                    return
                if done_items <= 0:
                    cache_hint = f"（已命中缓存 {_cached_hits} {ocr_unit}）" if _cached_hits > 0 else ""
                    self._emit_stage(
                        session,
                        task,
                        'extracting',
                        43,
                        f"正在对投标书扫描件/内嵌图片执行离线 OCR（已完成 0 / {total_items} {ocr_unit}，每 {flush_step} {ocr_unit}增量落盘缓存）{cache_hint}",
                    )
                    return
                now_ts = time.perf_counter()
                if done_items < total_items and (now_ts - last_ocr_emit[0]) < 1.5 and (done_items % 5 != 0):
                    return
                last_ocr_emit[0] = now_ts
                pct = min(53, 42 + int(round((done_items / max(1, total_items)) * 11)))
                self._emit_stage(
                    session,
                    task,
                    'extracting',
                    pct,
                    f"正在对投标书扫描件/内嵌图片执行离线 OCR（已完成 {done_items} / {total_items} {ocr_unit}，每 {flush_step} {ocr_unit}增量落盘缓存）",
                )

            bid_parsed = parse_document(str(bid_abs), ocr_images=True, progress_cb=_on_bid_ocr_progress)
            duration_record['f_parse_ms'] = max(0, int(round((time.perf_counter() - t_parse) * 1000)))

            # ── 阶段 2：checking（F2 报价沙盒 + F3 证照/串名 + F4 双书逐条响应核验）──
            self.check_cancelled(task_id, cancel_ev)
            self._emit_stage(session, task, 'checking', 55, 'F2/F3/F4 正在并行执行报价算术、证照效期与 ★/▲ 条款逐条核验')

            t_f2 = time.perf_counter()
            f2_res = run_price_check(
                bid_abs,
                tender_path=tender_abs,
                parsed_doc=bid_parsed,
                tender_parsed_doc=tender_parsed,
                client=client,
            )
            duration_record['f2_ms'] = max(0, int(round((time.perf_counter() - t_f2) * 1000)))

            self.check_cancelled(task_id, cancel_ev)
            t_f3 = time.perf_counter()
            # E15 §2.1：一次性构建投标书统一双轨页码上下文（直接复用 response_checker 单一事实源，自动适配 pdf/docx）
            bid_page_ctx = _build_unified_page_context(bid_abs)

            user_explicit_social = bool(cfg_snap.get('required_social_months'))
            effective_social_months, social_min_count = resolve_effective_social_requirement(
                tender_text=tender_parsed.text,
                open_date_str=open_date_str,
                configured_months=social_months,
                is_user_explicit=user_explicit_social,
            )

            f3_findings = list(
                scan_qualifications(
                    text=bid_parsed.text,
                    open_date=open_date_str,
                    required_social_months=effective_social_months,
                    forbidden_names=forbidden_names,
                    target_buyer=buyer_name,
                    client=client,
                    social_min_count=social_min_count,
                )
                or []
            )
            if open_date_fallback_used:
                f3_findings.append({
                    'code': 'OPEN_DATE_FALLBACK',
                    'severity': 'warning',
                    'quote': '',
                    'offset': None,
                    'message': f"未识别到开标日期，效期核验基于默认日期 {open_date_str}，请人工核对",
                })
            duration_record['f3_ms'] = max(0, int(round((time.perf_counter() - t_f3) * 1000)))

            self.check_cancelled(task_id, cancel_ev)
            t_f4 = time.perf_counter()

            def _on_f4_progress(done_cnt: int, total_cnt: int) -> None:
                self.check_cancelled(task_id, cancel_ev)

            f4_res = run_response_check(
                tender_path=tender_abs,
                bid_path=bid_abs,
                requirements=f1_requirements,
                client=client,
                tender_parsed_doc=tender_parsed,
                bid_parsed_doc=bid_parsed,
                progress_cb=_on_f4_progress,
            )
            duration_record['f4_ms'] = max(0, int(round((time.perf_counter() - t_f4) * 1000)))

            # ── 阶段 3：grading（风险分级汇总与自定义红线扫描）──
            self.check_cancelled(task_id, cancel_ev)
            self._emit_stage(session, task, 'grading', 85, '正在汇总一票否决废标项与扣分项分级')
            t_grading = time.perf_counter()

            session.query(BidFinding).filter_by(task_id=task.id).delete()
            new_findings: list[BidFinding] = []

            # (a) F2 报价算术发现（E15 §2.3：执行三级防前文重名锚定链，命中则写入 quote / quote_offset / page）
            for f2_item in f2_res.get('findings') or []:
                f2_quote, f2_offset = locate_f2_finding_in_bid_text(f2_item, f2_res, bid_parsed.text)
                f2_phys_page, f2_footer_page, f2_is_docx = _resolve_unified_page(f2_offset, bid_page_ctx)
                f2_detail = _enrich_detail_with_page_meta(f2_item, f2_phys_page, f2_footer_page, f2_is_docx)
                new_findings.append(
                    BidFinding(
                        task_id=task.id,
                        source_module='F2',
                        code=str(f2_item.get('code') or 'PRICE_ERROR'),
                        severity=str(f2_item.get('severity') or 'error'),
                        category='报价算术核验',
                        clause_no='开标一览表/分项报价表',
                        quote=f2_quote,
                        quote_offset=f2_offset,
                        quote_verified=bool(f2_quote is not None),
                        page=f2_phys_page,
                        message=str(f2_item.get('message') or '报价算术不一致'),
                        suggestion='请核对分项单价×数量、合计及大小写金额使之完全一致',
                        consequence='veto' if f2_item.get('severity', 'error') == 'error' else 'deduction',
                        detail_json=f2_detail,
                    )
                )

            # (b) F3 证照效期与串名穿帮发现（E15 §2.1：复用统一页码上下文写入 page 与双轨页码详情）
            for f3_item in f3_findings:
                f3_offset = f3_item.get('offset')
                f3_phys_page, f3_footer_page, f3_is_docx = _resolve_unified_page(f3_offset, bid_page_ctx)
                f3_detail = _enrich_detail_with_page_meta(f3_item, f3_phys_page, f3_footer_page, f3_is_docx)
                new_findings.append(
                    BidFinding(
                        task_id=task.id,
                        source_module='F3',
                        code=str(f3_item.get('code') or 'QUAL_ERROR'),
                        severity=str(f3_item.get('severity') or 'error'),
                        category='证照效期与串名扫描',
                        clause_no='资格证明文件',
                        quote=str(f3_item.get('quote') or ''),
                        quote_offset=f3_offset,
                        quote_verified=False,
                        page=f3_phys_page,
                        message=str(f3_item.get('message') or ''),
                        suggestion=resolve_f3_suggestion(f3_item),
                        consequence='veto' if f3_item.get('severity') == 'error' else 'deduction',
                        detail_json=f3_detail,
                    )
                )

            # (b2) E23：手填开标日期与招标文件原文提取日期不一致时，发出双向冲突预警（仍按手填日期核验）
            if open_date_mismatch is not None:
                raw_ev_quote = str(open_date_mismatch.get('quote') or '').strip()
                ev_offset = open_date_mismatch.get('offset')
                ev_verified = bool(raw_ev_quote and verify_quote(raw_ev_quote, tender_parsed.text))
                if not ev_verified:
                    raw_ev_quote = ''
                    ev_offset = None

                t_phys_page: Optional[int] = None
                t_footer_page: Optional[int] = None
                t_is_docx = False
                if ev_verified and isinstance(ev_offset, int):
                    tender_page_ctx = _build_unified_page_context(tender_abs)
                    t_phys_page, t_footer_page, t_is_docx = _resolve_unified_page(
                        ev_offset, tender_page_ctx
                    )

                mismatch_detail: dict[str, Any] = {
                    'manual_open_date': open_date_mismatch['manual_open_date'],
                    'detected_open_date': open_date_mismatch['detected_open_date'],
                    'quote_source': 'tender',
                }
                if t_phys_page is not None:
                    mismatch_detail['tender_page'] = t_phys_page
                    mismatch_detail['tender_footer_page'] = t_footer_page
                    mismatch_detail['tender_is_docx'] = t_is_docx

                m_date_user = open_date_mismatch['manual_open_date']
                m_date_doc = open_date_mismatch['detected_open_date']
                new_findings.append(
                    BidFinding(
                        task_id=task.id,
                        source_module='F3',
                        code='OPEN_DATE_MISMATCH',
                        severity='warning',
                        category='开标日期冲突核验',
                        clause_no='开标时间 / 递交截止时间',
                        quote=raw_ev_quote,
                        quote_offset=ev_offset,
                        quote_verified=ev_verified,
                        page=t_phys_page,
                        message=(
                            f"手填开标日期（{m_date_user}）与招标文件原文识别的开标日期（{m_date_doc}）不一致，"
                            f"本次 F3 效期核验按手填日期执行；若无官方延期/更正补充公告，请核实是否填错或识别有误"
                        ),
                        suggestion=(
                            f"请核对招标文件原条款或最新更正公告：若以招标书原文（{m_date_doc}）为准，"
                            f"请修正开标日期后重跑体检；若确已延期至 {m_date_user}，可忽略本预警"
                        ),
                        consequence='deduction',
                        detail_json=mismatch_detail,
                    )
                )

            # (c) F4 条款响应核验、缺佐证与标书前后矛盾发现（E5：按条目符号动态生成 category 与 suggestion）
            for f4_item in f4_res.get('findings') or []:
                code_str = str(f4_item.get('code') or 'MISSING_RESPONSE')
                sev_str = str(f4_item.get('severity') or 'error')
                dyn_category, dyn_suggestion = resolve_f4_category_and_suggestion(f4_item)
                new_findings.append(
                    BidFinding(
                        task_id=task.id,
                        source_module='F4',
                        code=code_str,
                        severity=sev_str,
                        category=dyn_category,
                        clause_no=str(f4_item.get('clause_no') or f4_item.get('mark') or ''),
                        quote=str(f4_item.get('tender_quote') or ''),
                        bid_quote=str(f4_item.get('bid_quote') or ''),
                        quote_offset=f4_item.get('bid_start') if f4_item.get('bid_start') is not None else f4_item.get('tender_start'),
                        quote_verified=bool(f4_item.get('quote_verified', True)),
                        page=f4_item.get('bid_page') if f4_item.get('bid_page') is not None else f4_item.get('tender_page'),
                        message=str(f4_item.get('message') or ''),
                        suggestion=dyn_suggestion,
                        consequence='none' if sev_str == 'info' else ('veto' if sev_str == 'error' else 'deduction'),
                        detail_json=f4_item,
                    )
                )

            # (c2) 将 F4 阶段 B 单条大模型异常降级项（llm_status='error_fallback'）暴露为提示级 finding 供人工复核
            for star_item in f4_res.get('star_results') or []:
                if star_item.get('llm_status') != 'error_fallback':
                    continue
                fb_category, _ = resolve_f4_category_and_suggestion(star_item)
                new_findings.append(
                    BidFinding(
                        task_id=task.id,
                        source_module='F4',
                        code='LLM_FALLBACK',
                        severity='info',
                        category=f"{fb_category}（大模型降级）",
                        clause_no=str(star_item.get('clause_no') or star_item.get('mark') or ''),
                        quote=str(star_item.get('tender_quote') or ''),
                        bid_quote=str(star_item.get('bid_quote_raw') or star_item.get('bid_quote') or ''),
                        quote_offset=(
                            star_item.get('bid_start')
                            if star_item.get('bid_start') is not None
                            else star_item.get('tender_start')
                        ),
                        quote_verified=bool(star_item.get('quote_verified', False)),
                        page=(
                            star_item.get('bid_page')
                            if star_item.get('bid_page') is not None
                            else star_item.get('tender_page')
                        ),
                        message=(
                            f"该条款大模型语义复核超时或异常降级（{star_item.get('reason') or 'error_fallback'}），"
                            f"已保留原文定位窗口，请人工终核响应是否充分"
                        ),
                        suggestion='请人工对照招标文件要求与我方投标书响应原文，确认该实质性条款是否完整响应',
                        consequence='none',
                        detail_json=star_item,
                    )
                )

            # (d) 部门自定义关键词红线扫描（E15 §2.4：顺手补齐 page 字段）
            for cr in custom_redlines:
                kw = str((cr.get('keyword') if isinstance(cr, dict) else '') or '').strip()
                if kw and kw in bid_parsed.text:
                    pos = bid_parsed.text.find(kw)
                    lo = max(0, pos - 25)
                    hi = min(len(bid_parsed.text), pos + len(kw) + 25)
                    cr_phys_page, cr_footer_page, cr_is_docx = _resolve_unified_page(pos, bid_page_ctx)
                    cr_detail = _enrich_detail_with_page_meta(
                        cr if isinstance(cr, dict) else {},
                        cr_phys_page,
                        cr_footer_page,
                        cr_is_docx,
                    )
                    new_findings.append(
                        BidFinding(
                            task_id=task.id,
                            source_module='F3',
                            code='CUSTOM_REDLINE_HIT',
                            severity=str(cr.get('severity') or 'error'),
                            category='部门自定义红线',
                            clause_no='全文扫描',
                            quote=bid_parsed.text[lo:hi],
                            quote_offset=pos,
                            quote_verified=False,
                            page=cr_phys_page,
                            message=f"命中部门自定义禁用红线词：「{kw}」",
                            suggestion=f"请排查并清理标书中出现的禁用词「{kw}」",
                            consequence=str(cr.get('consequence') or 'veto'),
                            detail_json=cr_detail,
                        )
                    )

            # (e) E43 暗标防废标合规核查（0 Token，source_module='BLIND_BID'）
            blind_bid_findings: list[dict[str, Any]] = []
            if blind_bid_info.get('is_blind_bid'):
                tenant_row = session.query(Tenant).filter_by(id=task.tenant_id).first()
                tenant_name = str(tenant_row.name or '').strip() if tenant_row else ''
                raw_hints = list(forbidden_names) + list(cfg_snap.get('bidder_hints') or [])
                if (
                    tenant_name
                    and len(tenant_name) >= 4
                    and tenant_name not in ('默认租户', '默认部门', '系统默认部门', 'Default Tenant')
                ):
                    raw_hints.append(tenant_name)
                blind_custom_words = [
                    str(w).strip()
                    for w in (cfg_snap.get('blind_custom_words') or [])
                    if str(w or '').strip()
                ]
                blind_bid_findings = scan_blind_bid_compliance(
                    bid_abs,
                    blind_bid_info,
                    bidder_hints=raw_hints,
                    custom_words=blind_custom_words,
                )
                for bf in blind_bid_findings:
                    b_off = bf.get('offset')
                    bf_phys, bf_footer, bf_docx = (
                        _resolve_unified_page(b_off, bid_page_ctx)
                        if b_off is not None
                        else (bf.get('page'), None, False)
                    )
                    bf_detail = _enrich_detail_with_page_meta(
                        dict(bf.get('detail_json') or {}),
                        bf_phys,
                        bf_footer,
                        bf_docx,
                    )
                    if bf.get('tender_quote'):
                        bf_detail['tender_quote'] = str(bf['tender_quote'])[:300]
                    new_findings.append(
                        BidFinding(
                            task_id=task.id,
                            source_module='BLIND_BID',
                            code=str(bf.get('code') or 'BLIND_BID_IDENTITY_LEAK'),
                            severity=str(bf.get('severity') or 'error'),
                            category='暗标防废标核查',
                            clause_no=str(bf.get('clause_no') or '暗标合规核查'),
                            quote=str(bf.get('bid_quote') or ''),
                            quote_offset=b_off,
                            quote_verified=bool(bf.get('quote_verified', False)),
                            page=bf_phys,
                            message=str(bf.get('message') or ''),
                            suggestion=str(bf.get('suggestion') or ''),
                            consequence=str(bf.get('consequence') or ('veto' if bf.get('severity') == 'error' else 'none')),
                            detail_json=bf_detail,
                        )
                    )

            session.add_all(new_findings)
            session.flush()
            duration_record['grading_ms'] = max(0, int(round((time.perf_counter() - t_grading) * 1000)))

            stage_sum_ms = sum(
                v for k, v in duration_record.items() if k != 'total_ms' and isinstance(v, int)
            )
            elapsed_total_ms = max(stage_sum_ms, int(round((time.perf_counter() - pipeline_t0) * 1000)))
            duration_record['total_ms'] = elapsed_total_ms

            err_cnt = sum(1 for f in new_findings if f.severity == 'error')
            warn_cnt = sum(1 for f in new_findings if f.severity == 'warning')
            align_sum = f4_res.get('alignment_summary') or {}

            stats_payload = {
                'total_findings': len(new_findings),
                'error_count': err_cnt,
                'warning_count': warn_cnt,
                'f1_extracted_count': len(f1_requirements),
                'total_requirements': int(align_sum.get('total_requirements', 0)),
                'aligned_hits': int(align_sum.get('aligned_hits', 0)),
                'hit_rate': float(align_sum.get('hit_rate', 1.0)),
                'is_blind_bid': bool(blind_bid_info.get('is_blind_bid')),
                'blind_toc_forbidden': bool(blind_bid_info.get('toc_forbidden')),
                'blind_bid_finding_count': len(blind_bid_findings),
                'price_status': str(f2_res.get('status') or 'NEED_MANUAL'),
                'price_cap': f2_res.get('price_cap'),
                'bid_total': f2_res.get('bid_total'),
                'reference_bid_total': f2_res.get('reference_bid_total'),
                'table_total': f2_res.get('table_total'),
                'bid_total_upper': f2_res.get('bid_total_upper'),
                'price_items': [
                    {
                        'name': str(it.get('name') or ''),
                        'qty': str(it.get('qty') or '1'),
                        'unit_price': str(it.get('unit_price') or ''),
                        'total_price': str(it.get('total_price') or ''),
                    }
                    for it in (f2_res.get('items') or [])
                ],
                'llm_status': llm_status,
            }

            # E27 补强：F4 完成后零 LLM 预生成 F5 偏离表与统计快照，确保点进 F5 与交付打包秒开
            self.check_cancelled(task_id, cancel_ev)
            try:
                build_and_cache_deviation_artifacts(
                    task_id=task.id,
                    tender_abs=tender_abs,
                    bid_abs=bid_abs,
                    requirements=f1_requirements,
                    client=None,
                    f4_result=f4_res,
                )
            except Exception as dev_exc:
                _logger.warning("Task %s F5 偏离表预生成跳过（回退按需生成）: %s", task.id, dev_exc)

            # ── 阶段 4：delivered（核验完成）──
            self.check_cancelled(task_id, cancel_ev)
            finished_utc = _utc_now()
            if task.started_at is not None and finished_utc < task.started_at:
                finished_utc = task.started_at
            task.stats_json = stats_payload
            task.llm_usage_json = {'llm_status': llm_status}
            task.status = 'delivered'
            task.progress = 100
            task.stage_detail = f"体检完成：检出 {err_cnt} 处一票否决项、{warn_cnt} 处预警项"
            task.finished_at = finished_utc
            task.duration_json = json.dumps(duration_record, ensure_ascii=False)
            task.delivered_at = finished_utc
            task.updated_at = finished_utc
            session.commit()

            self.broadcaster.publish(
                task.id,
                'done',
                {
                    'task_id': task.id,
                    'status': 'delivered',
                    'stats': stats_payload,
                    'started_at': task.started_at.isoformat() if task.started_at else None,
                    'finished_at': finished_utc.isoformat(),
                    'updated_at': finished_utc.isoformat(),
                    'duration': duration_record,
                },
            )
        except _PipelineCancelled:
            session.rollback()
            with self._lock:
                is_latest_run = self._cancel_flags.get(task_id) is cancel_ev
            if not is_latest_run:
                return
            _cleanup_task_deviation_artifacts(task_id)
            stage_sum_ms = sum(
                v for k, v in duration_record.items() if k != 'total_ms' and isinstance(v, int)
            )
            duration_record['total_ms'] = max(stage_sum_ms, int(round((time.perf_counter() - pipeline_t0) * 1000)))
            finished_utc = _utc_now()
            try:
                session.query(BidFinding).filter_by(task_id=task_id).delete(synchronize_session=False)
                task = session.query(BidCheckTask).filter_by(id=task_id).first()
                if task is not None:
                    if task.started_at is None:
                        task.started_at = finished_utc
                    elif finished_utc < task.started_at:
                        finished_utc = task.started_at
                    task.status = 'failed'
                    task.error_msg = '用户中断'
                    task.stage_detail = '用户中断'
                    task.stats_json = None
                    task.finished_at = finished_utc
                    task.duration_json = json.dumps(duration_record, ensure_ascii=False)
                    task.updated_at = finished_utc
                    cfg = dict(task.config_json or {})
                    cfg['cancelled_by_user'] = True
                    task.config_json = cfg
                    session.commit()
            except Exception:
                session.rollback()
            self.broadcaster.publish(
                task_id,
                'error',
                {
                    'task_id': task_id,
                    'status': 'failed',
                    'stage_detail': '用户中断',
                    'error_msg': '用户中断',
                    'finished_at': finished_utc.isoformat(),
                    'updated_at': finished_utc.isoformat(),
                    'duration': duration_record,
                },
            )
        except Exception as exc:
            session.rollback()
            err_msg = f"{type(exc).__name__}: {exc}"
            stage_sum_ms = sum(
                v for k, v in duration_record.items() if k != 'total_ms' and isinstance(v, int)
            )
            duration_record['total_ms'] = max(stage_sum_ms, int(round((time.perf_counter() - pipeline_t0) * 1000)))
            finished_utc = _utc_now()
            try:
                task = session.query(BidCheckTask).filter_by(id=task_id).first()
                if task is not None:
                    if task.started_at is None:
                        task.started_at = finished_utc
                    elif finished_utc < task.started_at:
                        finished_utc = task.started_at
                    task.status = 'failed'
                    task.error_msg = err_msg
                    task.stage_detail = f"核验异常中断：{err_msg}"
                    task.finished_at = finished_utc
                    task.duration_json = json.dumps(duration_record, ensure_ascii=False)
                    task.updated_at = finished_utc
                    session.commit()
            except Exception:
                session.rollback()
            self.broadcaster.publish(
                task_id,
                'error',
                {
                    'task_id': task_id,
                    'status': 'failed',
                    'error_msg': err_msg,
                    'finished_at': finished_utc.isoformat(),
                    'updated_at': finished_utc.isoformat(),
                    'duration': duration_record,
                },
            )
        finally:
            session.close()
            self.unregister_task_execution(task_id, cancel_ev)

    def wait_all(self, timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + float(timeout)
        with self._lock:
            while self._in_flight:
                rem = deadline - time.monotonic()
                if rem <= 0:
                    return False
                self._lock.wait(timeout=min(0.1, rem))
            return True

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            self._shutdown = True
        if wait:
            self.wait_all(timeout=15.0)
        self._executor.shutdown(wait=wait)


_worker_instance: Optional[BidTaskWorker] = None
_worker_lock = threading.Lock()


def get_worker(session_factory=None) -> BidTaskWorker:
    global _worker_instance
    with _worker_lock:
        if _worker_instance is None:
            _worker_instance = BidTaskWorker(session_factory=session_factory)
        elif session_factory is not None:
            _worker_instance.session_factory = session_factory
        return _worker_instance
