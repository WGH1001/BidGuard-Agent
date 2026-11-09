# -*- coding: utf-8 -*-
"""E12 · L2 混合检索激活与内网网关优雅降级卡单元测试（含 Gemini 交叉复查新增边界用例）。

覆盖任务卡 §3 与 §5 全部验收标准（断言严格，严禁空循环假绿）：
1. 配置了向量模型的 client（embed 方法 + models['embedding']）→ hybrid_retrieve_windows
   命中 level='level2_embedding_rerank'，返回融合得分结果；
2. embed 抛 404/Model not found/超时等异常 → 零报错，静默回退 level1_bm25_synonym；
3. client.models 无 'embedding' 键 → embed 一次都不被调用，回退 L1；
4. BIDGUARD_FORCE_ZERO_TOKEN=1 → 不触发 L2；
5. LLMClient.embed：build_client_from_user_config(model_embedding=...) 正确注入；
   未配置模型时 raise ValueError；OpenAI SDK 路径、requests 路径与数量守卫行为正确；
6. 缓存边界（§5 检查点 4 & 5）：
   - 切换不同维度向量模型（旧缓存 shape=(N,2) vs 新模型 shape=(N,4)）→ 自动作废旧缓存重算并命中 L2；
   - 磁盘缓存文件损坏（非合法 npy 字节流）→ 自动自愈重算并命中 L2。
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core import hybrid_retriever
from core.hybrid_retriever import (
    get_hybrid_stats,
    hybrid_retrieve_windows,
    reset_hybrid_stats,
)
from core.llm_gateway import build_client_from_user_config


DOC_TEXT = (
    '第一章 技术要求\n'
    '★1. 系统质保期不得低于三年，质保期内免费提供备件与上门维修服务。\n'
    '第二章 商务要求\n'
    '投标保证金金额为人民币贰万元整，于开标前递交。\n'
    '第三章 评分细则\n'
    '拟派项目经理须具备信息系统项目管理师证书。\n'
)


class EmbedClient:
    """带向量能力的 mock client（models 配置 + embed 方法，可注入异常或自定义维度）。"""

    def __init__(self, embed_fn=None, with_embedding_cfg: bool = True, dim: int = 2):
        self.models = {'embedding': 'fake-emb-model'} if with_embedding_cfg else {}
        self.embed_calls = 0
        self._embed_fn = embed_fn
        self._dim = dim

    def embed(self, texts):
        self.embed_calls += 1
        if self._embed_fn is not None:
            return self._embed_fn(texts)
        out = []
        for t in texts:
            vec = [0.0] * self._dim
            if '质保' in t:
                vec[0] = 1.0
            else:
                vec[min(1, self._dim - 1)] = 1.0
            out.append(vec)
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
    assert seen_inputs[-1] == ('embedding-3', ['甲文本', '乙文本'])
    assert vecs == [[0.5, 1.0], [1.5, 0.0]]
    usage = client.total_usage()
    assert usage['prompt_tokens'] == 7
    assert usage['calls'] == 1
    vecs2 = client.embed('单文本')
    assert seen_inputs[-1] == ('embedding-3', ['单文本'])
    assert len(vecs2) == 1 and vecs2[0] == [0.5, 1.0]


# ── 6. 交叉复查新增测试：缓存维度失配自动作废重算、损坏缓存自愈与 requests 路径 ────

def test_cache_dimension_mismatch_invalidates_and_recomputes():
    """§5 检查点 4：当磁盘缓存为 2 维向量而新模型返回 4 维向量时，自动作废旧缓存重算并命中 L2。"""
    reset_hybrid_stats()
    client_2d = EmbedClient(dim=2)
    hits_2d = hybrid_retrieve_windows('质保期几年', DOC_TEXT, client=client_2d, top_k=3)
    assert hits_2d and hits_2d[0]['level'] == 'level2_embedding_rerank'

    doc_sha = hashlib.sha256(DOC_TEXT.encode('utf-8')).hexdigest()
    cache_file = hybrid_retriever._CACHE_DIR / f"{doc_sha}.emb.npy"
    assert cache_file.is_file()
    assert np.load(str(cache_file)).shape[1] == 2

    # 切换为 4 维向量模型查询同一文档：旧 (N,2) 缓存应自动作废并重算为 (N,4)，绝不因 np.dot 异常退回 L1
    client_4d = EmbedClient(dim=4)
    hits_4d = hybrid_retrieve_windows('质保期几年', DOC_TEXT, client=client_4d, top_k=3)
    assert hits_4d and hits_4d[0]['level'] == 'level2_embedding_rerank'
    assert client_4d.embed_calls == 2  # 1 次 q_vec + 1 次重算 doc_vecs
    assert np.load(str(cache_file)).shape[1] == 4


def test_corrupted_cache_file_recovers_and_recomputes():
    """§5 检查点 5：磁盘 .emb.npy 缓存文件损坏时，自动忽略损坏文件并重算命中 L2。"""
    reset_hybrid_stats()
    doc_sha = hashlib.sha256(DOC_TEXT.encode('utf-8')).hexdigest()
    cache_dir = hybrid_retriever._CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{doc_sha}.emb.npy"
    cache_file.write_bytes(b'corrupted_not_a_valid_npy_file')

    client = EmbedClient(dim=3)
    hits = hybrid_retrieve_windows('质保期几年', DOC_TEXT, client=client, top_k=3)
    assert hits and hits[0]['level'] == 'level2_embedding_rerank'
    assert np.load(str(cache_file)).shape[1] == 3


def test_llm_client_embed_requests_fallback_and_count_guard():
    """§5 检查点 1 & 5：验证 requests 兜底分支与返回向量数量不一致时的 RuntimeError 守卫。"""
    client = build_client_from_user_config(
        base_url='https://x.example/v1', api_key='k', model_flash='m1',
        model_embedding='embedding-3',
    )

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _FakeRequests:
        def __init__(self, payload):
            self.payload = payload
            self.last_timeout = None

        def post(self, url, headers=None, json=None, timeout=None):
            self.last_timeout = timeout
            return _FakeResp(self.payload)

    fake_req = _FakeRequests({
        'data': [
            {'index': 1, 'embedding': [0.3, 0.4]},
            {'index': 0, 'embedding': [0.1, 0.2]},
        ],
        'usage': {'prompt_tokens': 5},
    })
    client.client = fake_req
    client.use_openai = False

    vecs = client.embed(['文本1', '文本2'])
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]
    assert fake_req.last_timeout == 15

    # 返回数量与输入不一致时应抛 RuntimeError
    client.client = _FakeRequests({'data': [{'index': 0, 'embedding': [0.1, 0.2]}], 'usage': {}})
    with pytest.raises(RuntimeError, match='数量与输入不一致'):
        client.embed(['文本1', '文本2'])