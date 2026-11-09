# -*- coding: utf-8 -*-
"""E12 · L2 混合检索激活与内网网关优雅降级卡单元测试。

覆盖任务卡 §3 全部验收标准（断言严格，严禁空循环假绿）：
1. 配置了向量模型的 client（embed 方法 + models['embedding']）→ hybrid_retrieve_windows
   命中 level='level2_embedding_rerank'，返回融合得分结果；
2. embed 抛 404/Model not found/超时等异常 → 零报错，静默回退 level1_bm25_synonym；
3. client.models 无 'embedding' 键 → embed 一次都不被调用，回退 L1；
4. BIDGUARD_FORCE_ZERO_TOKEN=1 → 不触发 L2；
5. LLMClient.embed：build_client_from_user_config(model_embedding=...) 正确注入；
   未配置模型时 raise ValueError；OpenAI SDK 路径与数量守卫行为正确。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core import hybrid_retriever
from core.hybrid_retriever import (
    get_hybrid_stats,
    hybrid_retrieve_windows,
    reset_hybrid_stats,
)
from core.llm_gateway import LLMClient, build_client_from_user_config


DOC_TEXT = (
    '第一章 技术要求\n'
    '★1. 系统质保期不得低于三年，质保期内免费提供备件与上门维修服务。\n'
    '第二章 商务要求\n'
    '投标保证金金额为人民币贰万元整，于开标前递交。\n'
    '第三章 评分细则\n'
    '拟派项目经理须具备信息系统项目管理师证书。\n'
)


class EmbedClient:
    """带向量能力的 mock client（models 配置 + embed 方法，可注入异常）。"""

    def __init__(self, embed_fn=None, with_embedding_cfg: bool = True):
        self.models = {'embedding': 'fake-emb-model'} if with_embedding_cfg else {}
        self.embed_calls = 0
        self._embed_fn = embed_fn

    def embed(self, texts):
        self.embed_calls += 1
        if self._embed_fn is not None:
            return self._embed_fn(texts)
        # 默认：含"质保"的文本与查询向量相近，其余正交
        out = []
        for t in texts:
            if '质保' in t:
                out.append([1.0, 0.0])
            else:
                out.append([0.0, 1.0])
        return out


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    """每个用例独立缓存目录，严防 SHA256 向量缓存跨用例串扰。"""
    monkeypatch.setattr(hybrid_retriever, '_CACHE_DIR', Path(tmp_path) / 'cache')
    yield


# ── 1. 配置向量模型 → 命中 level2_embedding_rerank ──────────────────────────

def test_level2_activated_with_embedding_model():
    reset_hybrid_stats()
    client = EmbedClient()
    hits = hybrid_retrieve_windows('质保期几年', DOC_TEXT, client=client, top_k=3)
    assert hits, '应返回非空检索结果'
    assert client.embed_calls >= 1, '配置了向量模型时应真实调用 embed'
    assert get_hybrid_stats()['level2_calls'] >= 1
    # 命中"质保期"语义行且 level 标记为 L2 精排
    top = hits[0]
    assert top['level'] == 'level2_embedding_rerank'
    assert '质保' in top['quote']
    assert isinstance(top['score'], float) and top['score'] > 0


# ── 2. embed 异常（404/超时）→ 零报错静默回退 L1 ─────────────────────────────

@pytest.mark.parametrize('exc', [
    RuntimeError('404 Model not found: fake-emb-model'),
    TimeoutError('embedding 请求超时'),
    ValueError('未配置 embedding 向量模型'),
])
def test_embed_error_gracefully_falls_back_to_level1(exc):
    reset_hybrid_stats()
    client = EmbedClient(embed_fn=lambda texts: (_ for _ in ()).throw(exc))
    hits = hybrid_retrieve_windows('质保期几年', DOC_TEXT, client=client, top_k=3)
    # 严禁上抛：必须拿到 Level 1 结果
    assert hits, '异常后应静默回退 Level 1 并返回 BM25 结果'
    for h in hits:
        assert h['level'] == 'level1_bm25_synonym'
    assert get_hybrid_stats()['level2_calls'] == 0


# ── 3. 未配置向量模型 → embed 零调用，直接停留 L1 ────────────────────────────

def test_no_embedding_cfg_never_calls_embed():
    reset_hybrid_stats()
    client = EmbedClient(with_embedding_cfg=False)
    hits = hybrid_retrieve_windows('质保期几年', DOC_TEXT, client=client, top_k=3)
    assert hits
    assert client.embed_calls == 0, '未配置 embedding 时严禁发起向量请求'
    assert get_hybrid_stats()['level2_calls'] == 0
    for h in hits:
        assert h['level'] == 'level1_bm25_synonym'


# ── 4. BIDGUARD_FORCE_ZERO_TOKEN=1 → 强制 L1 ────────────────────────────────

def test_force_zero_token_env_disables_level2(monkeypatch):
    reset_hybrid_stats()
    monkeypatch.setenv('BIDGUARD_FORCE_ZERO_TOKEN', '1')
    client = EmbedClient()
    hits = hybrid_retrieve_windows('质保期几年', DOC_TEXT, client=client, top_k=3)
    assert client.embed_calls == 0
    assert get_hybrid_stats()['level2_calls'] == 0
    for h in hits:
        assert h['level'] == 'level1_bm25_synonym'


# ── 5. LLMClient.embed 网关实现 ──────────────────────────────────────────────

def test_build_client_from_user_config_injects_embedding():
    client = build_client_from_user_config(
        base_url='https://open.bigmodel.cn/api/paas/v4',
        api_key='fake-key',
        model_flash='glm-5.3-flash',
        model_embedding='embedding-3',
    )
    assert client.models.get('embedding') == 'embedding-3'
    # 未传 model_embedding 时不配置该键（天然降级）
    client2 = build_client_from_user_config(
        base_url='https://x.example/v1', api_key='k', model_flash='m1',
    )
    assert 'embedding' not in client2.models


def test_llm_client_embed_raises_without_model():
    client = build_client_from_user_config(
        base_url='https://x.example/v1', api_key='k', model_flash='m1',
    )
    with pytest.raises(ValueError):
        client.embed('任意文本')


def test_llm_client_embed_openai_sdk_path():
    """OpenAI SDK 路径：mock client.embeddings.create 验证返回向量与用量计量。"""
    client = build_client_from_user_config(
        base_url='https://x.example/v1', api_key='k', model_flash='m1',
        model_embedding='embedding-3',
    )
    # 构造 fake OpenAI embeddings 响应（乱序 data 验证 index 排序；记录入参供断言）
    seen_inputs: list = []

    def _make_fake_resp(input_texts):
        n = len(input_texts)
        return SimpleNamespace(
            data=[
                SimpleNamespace(index=i, embedding=[0.5 + i, 1.0 - i])
                for i in range(n)
            ],
            usage=SimpleNamespace(prompt_tokens=7),
        )

    class _FakeEmbeddings:
        def create(self, model, input):
            seen_inputs.append((model, list(input)))
            return _make_fake_resp(input)

    class _FakeOpenAI:
        embeddings = _FakeEmbeddings()

    client.client = _FakeOpenAI()
    client.use_openai = True

    vecs = client.embed(['甲文本', '乙文本'])
    assert seen_inputs[-1] == ('embedding-3', ['甲文本', '乙文本']), '模型名与入参列表应原样透传'
    assert vecs == [[0.5, 1.0], [1.5, 0.0]], '应按 index 排序返回向量'
    usage = client.total_usage()
    assert usage['prompt_tokens'] == 7
    assert usage['calls'] == 1
    # 字符串入参归一为单元素列表
    vecs2 = client.embed('单文本')
    assert seen_inputs[-1] == ('embedding-3', ['单文本'])
    assert len(vecs2) == 1 and vecs2[0] == [0.5, 1.0]
