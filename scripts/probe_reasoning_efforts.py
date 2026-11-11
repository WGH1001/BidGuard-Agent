#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""E22 §2.4 · 网关思考参数实弹探针工具（仅供人工在有网/公司内网环境手动运行，严禁被 pytest/CI import）。

用法示例：
  1. 按 config/models.yaml 环境名探测（自动读取对应 api_key_env 环境变量）：
     python scripts/probe_reasoning_efforts.py --env prod
     python scripts/probe_reasoning_efforts.py --env demo

  2. 显式指定网关地址、模型与 Key 直连探测：
     python scripts/probe_reasoning_efforts.py --base-url http://10.0.2.81:3000/v1 --model GLM-5.3-Flash --api-key sk-xxx
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time
from typing import Any
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / 'config' / 'models.yaml'
CANDIDATE_EFFORTS = ('low', 'medium', 'high', 'max')


def _mask_key(raw_key: str) -> str:
    """脱敏显示 API Key（严禁在终端或日志打印明文 Key）。"""
    s = (raw_key or '').strip()
    if len(s) <= 8:
        return '***REDACTED***'
    return f"{s[:3]}***{s[-4:]}"


def _resolve_target_config(args: argparse.Namespace) -> tuple[str, str, str]:
    """解析 (base_url, api_key, model_name)。"""
    if args.base_url and args.model:
        api_key = args.api_key or os.environ.get('PRIVATE_API_KEY') or os.environ.get('ZHIPU_API_KEY') or ''
        if not api_key:
            raise RuntimeError('直连模式请通过 --api-key 或环境变量 PRIVATE_API_KEY / ZHIPU_API_KEY 提供密钥')
        return args.base_url.rstrip('/'), api_key.strip(), args.model.strip()

    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        cfg = yaml.safe_load(f) or {}

    env_name = args.env or cfg.get('default_env') or 'demo'
    env_cfg = (cfg.get('envs') or {}).get(env_name)
    if not env_cfg:
        raise ValueError(f"config/models.yaml 中未找到环境配置: {env_name}")

    base_url = str(args.base_url or env_cfg.get('base_url') or '').rstrip('/')
    models_dict = env_cfg.get('models') or {}
    model_name = str(args.model or models_dict.get('flash') or models_dict.get('flagship') or '').strip()
    key_env = str(env_cfg.get('api_key_env') or 'PRIVATE_API_KEY')
    api_key = (args.api_key or os.environ.get(key_env) or '').strip()
    if not api_key:
        raise RuntimeError(f"环境变量 {key_env} 未设置，无法执行实弹探测")

    return base_url, api_key, model_name


def _probe_single_payload(base_url: str, api_key: str, model_name: str, extra_body: dict[str, Any]) -> tuple[str, int, str]:
    """使用 requests 发送最小 chat 请求（max_tokens=16），返回 (status_label, latency_ms, detail_summary)。"""
    import requests

    headers = {
        'Authorization': f"Bearer {api_key}",
        'Content-Type': 'application/json',
    }
    payload = {
        'model': model_name,
        'messages': [{'role': 'user', 'content': '请只回复数字1'}],
        'temperature': 0.1,
        'max_tokens': 16,
        **extra_body,
    }
    t0 = time.perf_counter()
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload,
            timeout=20,
        )
        ms = int(round((time.perf_counter() - t0) * 1000))
        if resp.status_code == 200:
            data = resp.json()
            content = (
                ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
            ).strip().replace('\n', ' ')
            return 'HTTP 200 OK', ms, f"content={content[:30]!r}"
        err_txt = (resp.text or '').strip().replace('\n', ' ')
        return f"HTTP {resp.status_code}", ms, err_txt[:120]
    except Exception as exc:
        ms = int(round((time.perf_counter() - t0) * 1000))
        return f"EXC ({type(exc).__name__})", ms, str(exc)[:120]


def main() -> int:
    parser = argparse.ArgumentParser(description='E22 网关 reasoning_effort / enable_thinking 实弹探针')
    parser.add_argument('--env', type=str, default=None, help='models.yaml 中的环境名（如 demo / prod）')
    parser.add_argument('--base-url', type=str, default=None, help='覆盖网关 base_url')
    parser.add_argument('--api-key', type=str, default=None, help='覆盖 API Key（不在终端回显明文）')
    parser.add_argument('--model', type=str, default=None, help='覆盖测试模型名')
    args = parser.parse_args()

    try:
        base_url, api_key, model_name = _resolve_target_config(args)
    except Exception as e:
        print(f"[ERROR] 探针配置解析失败: {e}", file=sys.stderr)
        return 1

    print("=" * 76)
    print(f"BidGuard-Agent E22 网关思考参数探针")
    print(f"- Base URL : {base_url}")
    print(f"- Model    : {model_name}")
    print(f"- API Key  : {_mask_key(api_key)}")
    print("=" * 76)

    supported_efforts: list[str] = []
    for val in CANDIDATE_EFFORTS:
        status_lbl, ms, summary = _probe_single_payload(
            base_url, api_key, model_name, {'reasoning_effort': val}
        )
        if status_lbl == 'HTTP 200 OK':
            supported_efforts.append(val)
        print(f"[reasoning_effort={val:<6}] -> {status_lbl:<14} ({ms:>4} ms) | {summary}")

    st_think, ms_think, sum_think = _probe_single_payload(
        base_url, api_key, model_name, {'enable_thinking': True}
    )
    print(f"[enable_thinking=True  ] -> {st_think:<14} ({ms_think:>4} ms) | {sum_think}")
    print("-" * 76)
    print(f"探测结论：支持的 reasoning_effort 枚举值 = {supported_efforts}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
