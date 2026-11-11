# -*- coding: utf-8 -*-
"""T5 / T60 / E12 / E22 模型网关 [Gemini-Flash] —— 业务代码与模型/部署形态之间的唯一隔断层

实现规格（签名不得修改）：
    get_llm_client(env=None) -> LLMClient
    build_client_from_user_config(base_url, api_key, model_flash, model_flagship=None, supports_reasoning_effort=False, model_embedding=None) -> LLMClient
    LLMClient.chat(messages, model_tier='flagship', max_tokens=None) -> LLMResponse
    LLMClient.chat_stream(messages, model_tier='flagship', max_tokens=None) -> Iterator[dict]
    LLMClient.embed(texts) -> list[list[float]]
    LLMClient.total_usage() -> dict   # 累计用量
"""
import os
from pathlib import Path
import threading
import time
from typing import Iterator, Optional
import yaml

from core.interfaces import LLMResponse

CONFIG_PATH = Path(__file__).resolve().parents[1] / 'config' / 'models.yaml'

# E22 §2.1：思考相关参数键集合与内部非透传配置键集合
_THINKING_PARAM_KEYS = (
    'reasoning_effort',
    'thinking',
    'thinking_budget',
    'enable_thinking',
)
_INTERNAL_CHAT_OPTION_KEYS = (
    'max_tokens',
    'reasoning_effort_map',
)

# E22 §2.1：组合条件识别上游对思考参数的 400 拒绝（思考参数关键词 AND 拒绝语义词）
_THINKING_KEYWORDS_CF = (
    'reasoning_effort',
    'enable_thinking',
    'thinking_budget',
    'thinking',
)
_REJECT_SEMANTIC_WORDS_CF = (
    'invalid',
    'unsupported',
    'unknown',
    'not support',
    'unrecognized',
    'unexpected keyword',
    'extra fields',
    '非法',
    '无效',
    '不支持',
    '未知参数',
)


def get_llm_client(env=None) -> 'LLMClient':
    """按环境名构造客户端；env=None 时取 default_env。"""
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    if env is None:
        env = config.get('default_env')

    env_config = config.get('envs', {}).get(env)
    if not env_config:
        raise ValueError(f"环境中未找到配置: {env}")

    api_key_env = env_config.get('api_key_env')
    api_key = os.environ.get(api_key_env)

    if not api_key:
        raise RuntimeError(f'未配置环境变量 {api_key_env}，禁止将 Key 硬编码进代码')

    return LLMClient(env_config, api_key)


def build_client_from_user_config(
    base_url: str,
    api_key: str,
    model_flash: str,
    model_flagship: Optional[str] = None,
    supports_reasoning_effort: bool = False,
    model_embedding: Optional[str] = None,
) -> 'LLMClient':
    """T60 / E12 根据用户级自定义配置构造 LLMClient 实例（不改既有 get_llm_client 签名）。

    Args:
        base_url: OpenAI 兼容接口地址（如 https://api.deepseek.com/v1）。
        api_key: 解密后的明文 API Key（仅驻留内存，绝不落盘或打印）。
        model_flash: 快速模型名（用于问答、摘要提取、轻量评估）。
        model_flagship: 旗舰模型名（用于语义审查；缺省与 model_flash 相同）。
        supports_reasoning_effort: 是否支持 reasoning_effort 参数透传。
        model_embedding: 可选向量模型名（E12：配置后激活 L2 混合检索 Rerank；
            缺省 None = 不配置向量模型，检索自动停留 Level 1）。
    """
    if not base_url or not str(base_url).strip():
        raise ValueError("base_url 不能为空")
    if not api_key or not str(api_key).strip():
        raise ValueError("api_key 不能为空")
    if not model_flash or not str(model_flash).strip():
        raise ValueError("model_flash 不能为空")

    flash_clean = str(model_flash).strip()
    flagship_clean = str(model_flagship or flash_clean).strip() or flash_clean
    chat_options = {'reasoning_effort': 'low'} if supports_reasoning_effort else {}

    models_cfg = {
        'flash': flash_clean,
        'flagship': flagship_clean,
    }
    if model_embedding and str(model_embedding).strip():
        models_cfg['embedding'] = str(model_embedding).strip()

    env_config = {
        'provider': 'user_custom',
        'base_url': str(base_url).strip().rstrip('/'),
        'models': models_cfg,
        'chat_options': chat_options,
        'pricing': {},
    }
    client = LLMClient(env_config, str(api_key).strip())
    client.supports_reasoning_effort = bool(supports_reasoning_effort)
    return client


def _is_thinking_reject(e: Exception) -> bool:
    """识别上游对 thinking / reasoning_effort 等思考参数的 400 拒绝。

    E22 §2.1 升级规则：
    1. '1210'（智谱参数错误码）保留为独立捷径，单独命中即返回 True；
    2. 其余情况必须同时满足：(含思考参数关键词) AND (含拒绝/非法语义词)，
       严禁裸匹配 'unsupported' 误伤 'unsupported image type' / 'unsupported model'。

    典型场景：私有化端点不支持某种 thinking 参数形状——此时网关自动去参
    重试，保证「换部署形态只改配置」的承诺不被附加参数卡死。
    """
    msg = str(e or '')
    if not msg:
        return False
    msg_cf = msg.casefold()
    if '1210' in msg_cf:
        return True
    has_thinking_key = any(k in msg_cf for k in _THINKING_KEYWORDS_CF)
    if not has_thinking_key:
        return False
    has_reject_word = any(w in msg_cf for w in _REJECT_SEMANTIC_WORDS_CF)
    return has_reject_word


def _strip_thinking_params(extra_body: dict) -> dict:
    """E22 §2.1：返回剥除了全部思考相关参数后的新字典副本（严禁原地修改 self.chat_options）。"""
    if not extra_body:
        return {}
    return {
        k: v for k, v in extra_body.items()
        if k not in _THINKING_PARAM_KEYS
    }


class LLMClient:
    def __init__(self, env_config: dict, api_key: str):
        self.env_config = env_config
        self.api_key = api_key
        self.base_url = env_config.get('base_url')
        self.models = env_config.get('models', {})
        self.pricing = env_config.get('pricing', {})
        self.chat_options = env_config.get('chat_options', {}) or {}
        self._lock = threading.Lock()  # 并发批次下的用量累加保护

        self._usage = {
            'prompt_tokens': 0,
            'completion_tokens': 0,
            'cost': 0.0,
            'calls': 0
        }

        try:
            from openai import OpenAI
            self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            self.use_openai = True
        except ImportError:
            import requests
            self.client = requests
            self.use_openai = False

    def _mark_effort_unsupported(self) -> None:
        """E22 §2.1：单实例内记录上游已拒绝思考参数（熔断记忆仅存活于当前实例内存）。"""
        self.supports_reasoning_effort = False

    def translate_reasoning_effort(self, value: str) -> Optional[str]:
        """E22 §2.1.6：读取 chat_options.reasoning_effort_map 做 UI 挡位值 -> 上游值翻译。

        当 chat_options 未配置 reasoning_effort_map 或 value 不在映射表中时，
        严格返回 None（交由调用方安全回退至内置 _EFFORT_UPSTREAM_MAP，严禁原样返回 'medium'）。
        """
        if not value or not str(value).strip():
            return None
        raw_map = self.chat_options.get('reasoning_effort_map')
        if not isinstance(raw_map, dict) or not raw_map:
            return None
        norm_key = str(value).strip().lower()
        for k, v in raw_map.items():
            if str(k).strip().lower() == norm_key and v is not None:
                mapped = str(v).strip().lower()
                return mapped if mapped else None
        return None

    def _resolve_max_tokens(self, max_tokens: Optional[int]) -> Optional[int]:
        """解析生效的 max_tokens（显式传参优先，其次读 chat_options.max_tokens，均无返 None）。"""
        if max_tokens is not None:
            return int(max_tokens)
        cfg_mt = self.chat_options.get('max_tokens')
        if cfg_mt is not None:
            return int(cfg_mt)
        return None

    def _build_extra_body(self) -> dict:
        """构建请求附加参数：chat_options 通用透传（max_tokens 与 reasoning_effort_map 除外）。

        v2.0.4：glm-5.3-flash 为强制思考型模型且思考量不可控——
        thinking.budget / thinking.effort 均被上游静默忽略，思考可烧穿
        max_tokens 导致 content 为空（finish=length）。实测上游支持
        OpenAI 风格 reasoning_effort=low 直出完整答案，故 yaml 配
        chat_options.reasoning_effort: low 即生效。本方法把 chat_options
        中除 max_tokens（由 _resolve_max_tokens 消费）外的键全部透传，
        历史兼容：thinking_budget 旧键映射为 thinking dict。

        E22 加固：
        - 无条件剔除内部配置键 reasoning_effort_map，绝不透传给上游 HTTP 请求；
        - 当 getattr(self, 'supports_reasoning_effort', True) 为 False（含熔断后）时，
          同步跳过 reasoning_effort / enable_thinking / thinking / thinking_budget。
        """
        effort_supported = getattr(self, 'supports_reasoning_effort', True)
        extra_body = {}
        for key, value in self.chat_options.items():
            if key in _INTERNAL_CHAT_OPTION_KEYS:
                continue
            if not effort_supported and key in _THINKING_PARAM_KEYS:
                continue
            extra_body[key] = value
        if 'thinking_budget' in extra_body:
            budget = extra_body.pop('thinking_budget')
            if effort_supported:
                extra_body['thinking'] = {'type': 'enabled', 'budget': int(budget)}
        return extra_body

    def _calc_and_record_usage(self, model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
        """计算单次调用成本并线程安全地累加至实例账本。"""
        model_pricing = self.pricing.get(model_name, {})
        price_prompt = model_pricing.get('prompt', 0.0)
        price_completion = model_pricing.get('completion', 0.0)
        cost = (prompt_tokens / 1000.0 * price_prompt) + (completion_tokens / 1000.0 * price_completion)

        with self._lock:
            self._usage['prompt_tokens'] += prompt_tokens
            self._usage['completion_tokens'] += completion_tokens
            self._usage['cost'] += cost
            self._usage['calls'] += 1

        return cost

    def chat(self, messages: list, model_tier: str = 'flagship', max_tokens: Optional[int] = None,
             reasoning_effort: Optional[str] = None) -> LLMResponse:
        """调用模型并返回 LLMResponse（含 token 与成本计量）。

        Args:
            messages: OpenAI 格式消息列表。
            model_tier: 'flagship' | 'flash'。
            max_tokens: 可选生成 token 硬上限（None 时读取 chat_options.max_tokens）。
            reasoning_effort: 可选思考档位覆盖（v2.0.6，'low'|'medium'|'high'）——
                传入时覆盖 models.yaml chat_options 里的默认值，不传用配置默认。
        """
        model_name = self.models.get(model_tier)
        if not model_name:
            raise ValueError(f"未找到 model_tier '{model_tier}' 对应的模型名")

        extra_body = self._build_extra_body()
        if reasoning_effort and getattr(self, 'supports_reasoning_effort', True):
            extra_body['reasoning_effort'] = reasoning_effort
        resolved_max_tokens = self._resolve_max_tokens(max_tokens)

        temperature = 0.2
        max_retries = 1

        for attempt in range(max_retries + 1):
            try:
                if self.use_openai:
                    req_kwargs = {
                        'model': model_name,
                        'messages': messages,
                        'temperature': temperature,
                        'extra_body': extra_body,
                    }
                    if resolved_max_tokens is not None:
                        req_kwargs['max_tokens'] = resolved_max_tokens

                    response = self.client.chat.completions.create(**req_kwargs)
                    content = response.choices[0].message.content
                    prompt_tokens = response.usage.prompt_tokens
                    completion_tokens = response.usage.completion_tokens
                else:
                    headers = {
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    }
                    payload = {
                        "model": model_name,
                        "messages": messages,
                        "temperature": temperature,
                        **extra_body
                    }
                    if resolved_max_tokens is not None:
                        payload["max_tokens"] = resolved_max_tokens

                    resp = self.client.post(
                        f"{self.base_url}/chat/completions",
                        headers=headers,
                        json=payload
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data['choices'][0]['message']['content']
                    prompt_tokens = data['usage']['prompt_tokens']
                    completion_tokens = data['usage']['completion_tokens']

                cost = self._calc_and_record_usage(model_name, prompt_tokens, completion_tokens)

                return LLMResponse(
                    content=content,
                    model=model_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_estimate=cost
                )

            except Exception as e:
                if attempt == max_retries:
                    raise RuntimeError(f"API 请求失败重试耗尽: {e}")
                # thinking/reasoning_effort 参数被上游拒绝（如私有化端点不支持
                # 或档位值非法）→ 剥除全部思考参数并标记实例熔断后重试，后续轮次全程无参直发
                if _is_thinking_reject(e):
                    extra_body = _strip_thinking_params(extra_body)
                    self._mark_effort_unsupported()
                time.sleep(1)

        raise RuntimeError("不可达状态")

    def chat_stream(
        self,
        messages: list,
        model_tier: str = 'flagship',
        max_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
    ) -> Iterator[dict]:
        """流式调用模型，逐 chunk 产出 {'type': 'delta', 'text': str}，
        结束时产出 {'type': 'final', 'response': LLMResponse}。

        reasoning_effort: 可选思考档位覆盖（v2.0.6），传入时覆盖 yaml 默认值。
        """
        model_name = self.models.get(model_tier)
        if not model_name:
            raise ValueError(f"未找到 model_tier '{model_tier}' 对应的模型名")

        extra_body = self._build_extra_body()
        if reasoning_effort and getattr(self, 'supports_reasoning_effort', True):
            extra_body['reasoning_effort'] = reasoning_effort
        resolved_max_tokens = self._resolve_max_tokens(max_tokens)
        temperature = 0.2

        if not self.use_openai:
            # 降级：若无 OpenAI SDK，退回同步请求后包装为单次 delta + final（E22：补传 reasoning_effort）
            sync_resp = self.chat(
                messages,
                model_tier=model_tier,
                max_tokens=resolved_max_tokens,
                reasoning_effort=reasoning_effort,
            )
            if sync_resp.content:
                yield {'type': 'delta', 'text': sync_resp.content}
            yield {'type': 'final', 'response': sync_resp}
            return

        def _build_stream_kwargs(cur_extra: dict) -> dict:
            kw = {
                'model': model_name,
                'messages': messages,
                'temperature': temperature,
                'extra_body': cur_extra,
                'stream': True,
            }
            if resolved_max_tokens is not None:
                kw['max_tokens'] = resolved_max_tokens
            return kw

        include_usage_supported = True

        def _create_stream(cur_extra: dict):
            nonlocal include_usage_supported
            kw = _build_stream_kwargs(cur_extra)
            if include_usage_supported:
                try:
                    return self.client.chat.completions.create(
                        **kw,
                        stream_options={'include_usage': True},
                    )
                except TypeError:
                    include_usage_supported = False
            return self.client.chat.completions.create(**kw)

        # 优先尝试带思考参数发起流；若遇思考参数 400 拒绝（即便先经历了 stream_options TypeError 降级），
        # 立即剥除全部思考参数 + 标记实例熔断后重试一次；非思考参数异常（如 500/网络断连）直接上抛
        try:
            stream_iter = _create_stream(extra_body)
        except Exception as e:
            if _is_thinking_reject(e):
                extra_body = _strip_thinking_params(extra_body)
                self._mark_effort_unsupported()
                stream_iter = _create_stream(extra_body)
            else:
                raise

        collected_chunks: list[str] = []
        prompt_tokens: Optional[int] = None
        completion_tokens: Optional[int] = None

        for chunk in stream_iter:
            usage_obj = getattr(chunk, 'usage', None)
            if usage_obj is not None:
                pt = getattr(usage_obj, 'prompt_tokens', None)
                ct = getattr(usage_obj, 'completion_tokens', None)
                if pt is not None:
                    prompt_tokens = int(pt)
                if ct is not None:
                    completion_tokens = int(ct)

            choices = getattr(chunk, 'choices', None) or []
            if choices:
                delta = getattr(choices[0], 'delta', None)
                text_piece = getattr(delta, 'content', None) if delta is not None else None
                if text_piece:
                    collected_chunks.append(text_piece)
                    yield {'type': 'delta', 'text': text_piece}

        full_text = "".join(collected_chunks)

        # 若上游流未返回 usage，按 chars // 2 估算
        if prompt_tokens is None:
            total_prompt_chars = sum(len(str(m.get('content') or '')) for m in (messages or []))
            prompt_tokens = max(1, total_prompt_chars // 2)
        if completion_tokens is None:
            completion_tokens = max(1, len(full_text) // 2) if full_text else 0

        cost = self._calc_and_record_usage(model_name, prompt_tokens, completion_tokens)
        final_resp = LLMResponse(
            content=full_text,
            model=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_estimate=cost,
        )
        yield {'type': 'final', 'response': final_resp}

    def total_usage(self) -> dict:
        """返回累计用量 {'prompt_tokens','completion_tokens','cost','calls'}。"""
        return self._usage.copy()

    def embed(self, texts) -> list:
        """E12：调用向量模型返回浮点向量列表（OpenAI 兼容 /v1/embeddings 规范）。

        Args:
            texts: 单个字符串或字符串列表。

        Returns:
            list[list[float]]：与输入顺序一一对应的向量列表。

        Raises:
            ValueError: 未在 models.yaml / 用户配置中配置 models.embedding 向量模型名
                （调用方 hybrid_retriever 捕获后静默降级 Level 1，不会中断业务）。
        """
        model_name = self.models.get('embedding')
        if not model_name:
            raise ValueError("未配置 embedding 向量模型（models.yaml 或用户配置的 models.embedding），"
                             "L2 混合检索不可用，调用方应降级 Level 1")

        if isinstance(texts, str):
            texts_list = [texts]
        else:
            texts_list = [str(t) for t in (texts or [])]
        if not texts_list:
            return []

        if self.use_openai:
            resp = self.client.embeddings.create(model=model_name, input=texts_list)
            data = sorted(resp.data, key=lambda d: getattr(d, 'index', 0))
            vecs = [list(map(float, d.embedding)) for d in data]
            usage_obj = getattr(resp, 'usage', None)
            prompt_tokens = int(getattr(usage_obj, 'prompt_tokens', 0) or 0)
        else:
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            resp = self.client.post(
                f"{self.base_url}/embeddings",
                headers=headers,
                json={"model": model_name, "input": texts_list},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            items = sorted(data.get('data', []), key=lambda d: d.get('index', 0))
            vecs = [[float(x) for x in d['embedding']] for d in items]
            usage_obj = data.get('usage') or {}
            prompt_tokens = int(usage_obj.get('prompt_tokens', 0) or 0)

        if len(vecs) != len(texts_list):
            raise RuntimeError(f"embeddings 返回数量与输入不一致：{len(vecs)} != {len(texts_list)}")

        # embedding 仅消耗输入 token（completion 计 0），计入用量账本
        self._calc_and_record_usage(model_name, prompt_tokens, 0)
        return vecs
