# -*- coding: utf-8 -*-
"""E22 · LLM 网关思考参数防御加固专项单元测试（0 Token 离线可跑，断言严格数数）。

覆盖任务卡 §2.5 全部 5 项验收标准：
1. 组合拒绝识别 (_is_thinking_reject)：
   - 正例（含思考参数关键词 + 拒绝词，或智谱 1210 错误码）返回 True；
   - 反例（'unsupported image type'、'unsupported model'、纯 500 网络超时文案）返回 False，严防误伤；
2. 同步 chat() 剥参重试与单实例熔断记忆：
   - Mock 上游首次抛 400（reasoning_effort invalid）-> chat() 自动剥参重试成功；
   - 同一 LLMClient 实例第 2 次调用 chat() 时，首次请求即不带任何思考参数（熔断记忆生效，不再撞墙）；
   - F4 多线程并发安全：_strip_thinking_params 不原地修改 self.chat_options；
3. 流式 chat_stream() 同构去参重试与补漏回归：
   - 先遇 stream_options TypeError 降级、再遇 400 思考参数拒绝时，chat_stream() 仍能捕获并剥参重试成功且触发熔断；
   - use_openai=False 回退分支调用 self.chat 时正确透传 reasoning_effort；
   - 裸 500 / 网络异常直接上抛，不进无意义剥参重试；
4. _build_extra_body() 内部配置隔离与熔断跳过：
   - 无论 supports_reasoning_effort 为 True 或 False，reasoning_effort_map 绝不进入 extra_body；
   - supports_reasoning_effort=False 时，yaml 配置的 reasoning_effort / enable_thinking / thinking_budget 均被拦截；
   - 缺省无 supports_reasoning_effort 属性时（env 路径初始态）保持原有透传行为不变；
5. 映射配置化 (translate_reasoning_effort & _resolve_effort_and_rounds)：
   - 配置了 reasoning_effort_map 时按配置翻译并在 _resolve_effort_and_rounds(..., client=client) 中生效；
   - 未配置 reasoning_effort_map 时 translate_reasoning_effort('medium') 返回 None，回退内置映射得 ('high', 7)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.bid_react_agent import _resolve_effort_and_rounds
from core.llm_gateway import (
    CONFIG_PATH,
    LLMClient,
    _is_thinking_reject,
    _strip_thinking_params,
)


def _make_test_client(chat_options: dict | None = None) -> LLMClient:
    env_cfg = {
        'provider': 'private',
        'base_url': 'http://127.0.0.1:9999/v1',
        'models': {'flagship': 'test-flagship', 'flash': 'test-flash'},
        'chat_options': dict(chat_options or {}),
        'pricing': {},
    }
    return LLMClient(env_cfg, api_key='fake-test-key')


# ── 1. 组合条件识别单测（正例 True / 反例 False）───────────────────────────

@pytest.mark.parametrize(
    'err_msg',
    [
        "Error code: 400 - {'error': {'message': 'reasoning_effort is not supported by this model'}}",
        "Error code: 400 - {'code': '1210', 'message': 'API 调用参数有误'}",
        "BadRequestError: enable_thinking unknown parameter in request body",
        "400 Bad Request: Extra fields not permitted: 'thinking_budget'",
        "请求失败：无效的 reasoning_effort 参数值 medium",
        "400: 不支持的参数 thinking",
    ],
)
def test_e22_is_thinking_reject_positive_cases(err_msg: str):
    assert _is_thinking_reject(RuntimeError(err_msg)) is True


@pytest.mark.parametrize(
    'err_msg',
    [
        "Error code: 400 - {'error': {'message': 'unsupported image type: image/bmp'}}",
        "Error code: 400 - {'error': {'message': 'unsupported model: gpt-99'}}",
        "Error code: 404 - Model not found",
        "500 Internal Server Error: upstream gateway timeout",
        "ConnectionResetError: [Errno 104] Connection reset by peer",
        "",
    ],
)
def test_e22_is_thinking_reject_negative_cases_not_misclassified(err_msg: str):
    assert _is_thinking_reject(RuntimeError(err_msg)) is False


# ── 2. 同步 chat() 剥参重试 + 单实例熔断记忆 + chat_options 不被原地篡改 ──────

def test_e22_sync_chat_strips_params_on_retry_and_memoizes_circuit_breaker(monkeypatch):
    monkeypatch.setattr('time.sleep', lambda _s: None)
    original_opts = {
        'reasoning_effort': 'low',
        'enable_thinking': True,
        'thinking_budget': 1024,
    }
    client = _make_test_client(chat_options=original_opts)

    sent_extra_bodies: list[dict] = []

    class _FakeCompletions:
        def create(self, **kwargs):
            eb = dict(kwargs.get('extra_body') or {})
            sent_extra_bodies.append(eb)
            if any(k in eb for k in ('reasoning_effort', 'enable_thinking', 'thinking')):
                raise RuntimeError("400 Bad Request: reasoning_effort is invalid for this endpoint")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='ok_sync'))],
                usage=SimpleNamespace(prompt_tokens=6, completion_tokens=2),
            )

    client.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
    client.use_openai = True

    # 第 1 次调用 chat()：首次请求带思考参数触发 400 -> 自动剥参重试成功，且标记实例熔断
    resp1 = client.chat([{'role': 'user', 'content': 'hello'}], model_tier='flash', reasoning_effort='high')
    assert resp1.content == 'ok_sync'
    assert len(sent_extra_bodies) == 2
    assert 'reasoning_effort' in sent_extra_bodies[0]
    assert sent_extra_bodies[1] == {}
    assert client.supports_reasoning_effort is False
    # 严禁原地修改 self.chat_options（保护 F4 多线程并发安全）
    assert 'reasoning_effort' in client.chat_options

    # 同实例第 2 次调用 chat()：因熔断记忆生效，首次请求即无任何思考参数（不再撞墙）
    resp2 = client.chat([{'role': 'user', 'content': 'round 2'}], model_tier='flash', reasoning_effort='high')
    assert resp2.content == 'ok_sync'
    assert len(sent_extra_bodies) == 3
    assert sent_extra_bodies[2] == {}


# ── 3. 流式 chat_stream() 补漏回归（含 stream_options TypeError + 400 拒参叠加）──

def test_e22_chat_stream_retries_after_type_error_and_thinking_reject():
    client = _make_test_client(chat_options={'reasoning_effort': 'low', 'enable_thinking': True})
    stream_calls: list[dict] = []

    class _FakeStreamCompletions:
        def create(self, **kwargs):
            stream_calls.append(dict(kwargs))
            # 第一道坎：不支持 stream_options 抛 TypeError
            if 'stream_options' in kwargs:
                raise TypeError("create() got an unexpected keyword argument 'stream_options'")
            # 第二道坎：不支持 reasoning_effort 抛 400
            eb = kwargs.get('extra_body') or {}
            if 'reasoning_effort' in eb or 'enable_thinking' in eb:
                raise RuntimeError("400 Bad Request: reasoning_effort is unsupported")
            # 剥参后返回正常流
            return iter([
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content='流式'))],
                    usage=None,
                ),
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content='成功'))],
                    usage=SimpleNamespace(prompt_tokens=8, completion_tokens=4),
                ),
            ])

    client.client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeStreamCompletions()))
    client.use_openai = True

    events = list(client.chat_stream([{'role': 'user', 'content': 'stream test'}], model_tier='flash', reasoning_effort='max'))
    deltas = [e['text'] for e in events if e['type'] == 'delta']
    finals = [e['response'] for e in events if e['type'] == 'final']

    assert ''.join(deltas) == '流式成功'
    assert len(finals) == 1 and finals[0].content == '流式成功'
    assert client.supports_reasoning_effort is False
    # 最后一次成功请求的 extra_body 已彻底清空思考参数
    assert stream_calls[-1].get('extra_body') == {}

    # 同实例第 2 次流式调用：直接无参发送
    calls_before = len(stream_calls)
    list(client.chat_stream([{'role': 'user', 'content': 'stream round 2'}], model_tier='flash', reasoning_effort='high'))
    assert stream_calls[calls_before].get('extra_body') == {}


def test_e22_chat_stream_raises_non_thinking_500_without_retry_and_passes_effort_in_fallback():
    client = _make_test_client(chat_options={'reasoning_effort': 'low'})
    call_count = 0

    class _Fail500Completions:
        def create(self, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("502 Bad Gateway: upstream connection refused")

    client.client = SimpleNamespace(chat=SimpleNamespace(completions=_Fail500Completions()))
    client.use_openai = True

    with pytest.raises(RuntimeError, match='502 Bad Gateway'):
        list(client.chat_stream([{'role': 'user', 'content': 'hi'}], model_tier='flash'))
    # 裸 500/502 非思考参数错误绝不进入剥参重试
    assert call_count == 1

    # 验证 use_openai=False 回退分支调用 self.chat 时正确透传 reasoning_effort
    client_no_sdk = _make_test_client()
    client_no_sdk.use_openai = False
    seen_effort_in_sync: list[str | None] = []

    def _spy_chat(messages, model_tier='flagship', max_tokens=None, reasoning_effort=None):
        seen_effort_in_sync.append(reasoning_effort)
        return SimpleNamespace(content='fallback_ok')

    client_no_sdk.chat = _spy_chat  # type: ignore[method-assign]
    evs = list(client_no_sdk.chat_stream([{'role': 'user', 'content': 'hi'}], model_tier='flash', reasoning_effort='max'))
    assert seen_effort_in_sync == ['max']
    assert evs[0] == {'type': 'delta', 'text': 'fallback_ok'}


# ── 4. _build_extra_body() 内部配置隔离与熔断开关验证 ─────────────────────

def test_e22_build_extra_body_isolates_internal_keys_and_respects_circuit_breaker():
    c = _make_test_client(
        chat_options={
            'max_tokens': 2048,
            'reasoning_effort': 'low',
            'enable_thinking': True,
            'thinking_budget': 512,
            'reasoning_effort_map': {'low': 'low', 'medium': 'medium', 'high': 'high'},
            'top_p': 0.9,
        }
    )
    # 初始态（未设 supports_reasoning_effort 属性，默认视为 True）：
    # max_tokens 与 reasoning_effort_map 必须被隔离剔除，top_p 与思考参数保留
    eb_init = c._build_extra_body()
    assert 'max_tokens' not in eb_init
    assert 'reasoning_effort_map' not in eb_init
    assert eb_init.get('top_p') == 0.9
    assert eb_init.get('reasoning_effort') == 'low'
    assert eb_init.get('thinking') == {'type': 'enabled', 'budget': 512}

    # 触发熔断后（supports_reasoning_effort = False）：所有思考参数一律不进 extra_body，但保留普通参数 top_p
    c._mark_effort_unsupported()
    eb_after = c._build_extra_body()
    assert eb_after == {'top_p': 0.9}
    assert _strip_thinking_params(eb_init) == {'top_p': 0.9}


# ── 5. 映射配置化与未配置安全回退验证 + models.yaml 语法校验 ──────────────

def test_e22_translate_reasoning_effort_and_agent_resolution():
    # (a) 未配置 reasoning_effort_map 时：translate_reasoning_effort('medium') 严格返回 None，
    #     _resolve_effort_and_rounds 回退至内置 _EFFORT_UPSTREAM_MAP 得 ('high', 7)
    c_default = _make_test_client(chat_options={'reasoning_effort': 'low'})
    assert c_default.translate_reasoning_effort('medium') is None
    assert _resolve_effort_and_rounds('medium', 7, client=c_default) == ('high', 7)
    assert _resolve_effort_and_rounds('medium', 7) == ('high', 7)
    assert _resolve_effort_and_rounds('invalid_gear', 7, client=c_default) == (None, 7)

    # (b) 配置了自定义 reasoning_effort_map 时：按配置映射生效
    c_custom = _make_test_client(
        chat_options={
            'reasoning_effort_map': {
                'low': 'minimal',
                'medium': 'medium',
                'high': 'high',
            }
        }
    )
    assert c_custom.translate_reasoning_effort('low') == 'minimal'
    assert c_custom.translate_reasoning_effort('medium') == 'medium'
    assert c_custom.translate_reasoning_effort('HIGH') == 'high'
    assert c_custom.translate_reasoning_effort('unknown') is None

    assert _resolve_effort_and_rounds('low', 7, client=c_custom) == ('minimal', 7)
    assert _resolve_effort_and_rounds('medium', 7, client=c_custom) == ('medium', 7)
    assert _resolve_effort_and_rounds('high', 7, client=c_custom) == ('high', 7)

    # (c) 校验 config/models.yaml 语法合法
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        parsed_yaml = yaml.safe_load(f)
    assert isinstance(parsed_yaml, dict) and 'envs' in parsed_yaml
