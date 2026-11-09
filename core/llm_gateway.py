# -*- coding: utf-8 -*-
"""T5 / T60 模型网关 [Gemini-Flash] —— 业务代码与模型/部署形态之间的唯一隔断层

实现规格（签名不得修改）：
    get_llm_client(env=None) -> LLMClient
    build_client_from_user_config(base_url, api_key, model_flash, model_flagship=None, supports_reasoning_effort=False) -> LLMClient
    LLMClient.chat(messages, model_tier='flagship', max_tokens=None) -> LLMResponse
    LLMClient.chat_stream(messages, model_tier='flagship', max_tokens=None) -> Iterator[dict]
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
    """T60 根据用户级自定义配置构造 LLMClient 实例（不改既有 get_llm_client 签名）。

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
    """识别上游对 thinking 参数的 400 拒绝（错误码 1210 / 文案含 thinking）。

    典型场景：私有化端点不支持某种 thinking 参数形状——此时网关自动去参
    重试，保证「换部署形态只改配置」的承诺不被附加参数卡死。
    """
    msg = str(e)
    return '1210' in msg or 'thinking' in msg.lower()


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

    def _resolve_max_tokens(self, max_tokens: Optional[int]) -> Optional[int]:
        """解析生效的 max_tokens（显式传参优先，其次读 chat_options.max_tokens，均无返 None）。"""
        if max_tokens is not None:
            return int(max_tokens)
        cfg_mt = self.chat_options.get('max_tokens')
        if cfg_mt is not None:
            return int(cfg_mt)
        return None

    def _build_extra_body(self) -> dict:
        """构建请求附加参数：chat_options 通用透传（max_tokens 除外）。

        v2.0.4：glm-5.3-flash 为强制思考型模型且思考量不可控——
        thinking.budget / thinking.effort 均被上游静默忽略，思考可烧穿
        max_tokens 导致 content 为空（finish=length）。实测上游支持
        OpenAI 风格 reasoning_effort=low 直出完整答案，故 yaml 配
        chat_options.reasoning_effort: low 即生效。本方法把 chat_options
        中除 max_tokens（由 _resolve_max_tokens 消费）外的键全部透传，
        历史兼容：thinking_budget 旧键映射为 thinking dict。
        """
        extra_body = {}
        for key, value in self.chat_options.items():
            if key == 'max_tokens':
                continue
            extra_body[key] = value
        if 'thinking_budget' in extra_body:
            budget = extra_body.pop('thinking_budget')
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
                # 或档位值非法）→ 去掉相关参数重试，业务不因附加参数被卡死
                if _is_thinking_reject(e):
                    extra_body.pop('thinking', None)
                    extra_body.pop('reasoning_effort', None)
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
            # 降级：若无 OpenAI SDK，退回同步请求后包装为单次 delta + final
            sync_resp = self.chat(messages, model_tier=model_tier, max_tokens=resolved_max_tokens)
            if sync_resp.content:
                yield {'type': 'delta', 'text': sync_resp.content}
            yield {'type': 'final', 'response': sync_resp}
            return

        base_kwargs = {
            'model': model_name,
            'messages': messages,
            'temperature': temperature,
            'extra_body': extra_body,
            'stream': True,
        }
        if resolved_max_tokens is not None:
            base_kwargs['max_tokens'] = resolved_max_tokens

        # 优先附带 stream_options={'include_usage': True}，若上游不支持则降级不带该参数
        try:
            stream_iter = self.client.chat.completions.create(
                **base_kwargs,
                stream_options={'include_usage': True},
            )
        except TypeError:
            stream_iter = self.client.chat.completions.create(**base_kwargs)
        except Exception as e:
            # thinking 参数被上游拒绝 → 去掉 thinking 参数重试（保底直连）
            if _is_thinking_reject(e) and base_kwargs.get('extra_body', {}).get('thinking'):
                base_kwargs = {k: v for k, v in base_kwargs.items() if k != 'extra_body'}
            stream_iter = self.client.chat.completions.create(**base_kwargs)

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