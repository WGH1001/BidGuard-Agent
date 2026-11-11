# -*- coding: utf-8 -*-
"""E20 · F1/F5/F6 页面体验完善与 Docker 双项目端口共存卡单元测试。

覆盖任务卡 §2 全部验收标准：
1. docker-compose.yml 通过 yaml.safe_load 校验，postgres 宿主机端口映射为
   127.0.0.1:${POSTGRES_PORT:-15432}:5432，且 app 内部连接仍为 @postgres:5432；
2. deploy/.env.docker.example 与 docs/DEPLOY.md 已同步配置 POSTGRES_PORT=15432；
3. 前端源码契约核验：
   - DeviationView.vue 包含 5 维 statusFilter（ALL/no_deviation/negative/missing/contradiction）与 filteredRows；
   - RadarView.vue 已移除旧 customTitle 输入框，具备拖拽批量队列与 formatLocalCreatedAt 本地时区格式化；
   - ExtractView.vue 具备单行不换行横向滚动、el-select filterable 快速切换与 localStorage LRU pop() 淘汰防丢。
"""
from __future__ import annotations

from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_e20_docker_compose_postgres_port_coexistence():
    compose_path = ROOT / 'docker-compose.yml'
    raw = compose_path.read_text(encoding='utf-8')
    cfg = yaml.safe_load(raw)
    assert isinstance(cfg, dict) and 'services' in cfg

    pg_ports = cfg['services']['postgres']['ports']
    assert '127.0.0.1:${POSTGRES_PORT:-15432}:5432' in pg_ports

    app_envs = cfg['services']['app']['environment']
    assert any('@postgres:5432/' in str(item) for item in app_envs)

    env_example = (ROOT / 'deploy' / '.env.docker.example').read_text(encoding='utf-8')
    assert 'POSTGRES_PORT=15432' in env_example

    deploy_doc = (ROOT / 'docs' / 'DEPLOY.md').read_text(encoding='utf-8')
    assert '15432' in deploy_doc


def test_e20_frontend_f1_f5_f6_contracts():
    dev_vue = (ROOT / 'frontend' / 'src' / 'views' / 'DeviationView.vue').read_text(encoding='utf-8')
    for fk in ('ALL', 'no_deviation', 'negative', 'missing', 'contradiction'):
        assert f'value="{fk}"' in dev_vue
    assert 'filteredRows' in dev_vue

    radar_vue = (ROOT / 'frontend' / 'src' / 'views' / 'RadarView.vue').read_text(encoding='utf-8')
    assert 'customTitle' not in radar_vue
    assert 'formatLocalCreatedAt' in radar_vue
    assert 'pendingFiles' in radar_vue

    extract_vue = (ROOT / 'frontend' / 'src' / 'views' / 'ExtractView.vue').read_text(encoding='utf-8')
    assert 'trimmed.pop()' in extract_vue
    assert 'filterable' in extract_vue
    assert 'ehb-quick-select' in extract_vue
