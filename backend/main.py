# -*- coding: utf-8 -*-
"""B2 / E4 · FastAPI 主应用装配入口（CORS、11 组路由挂载、启动建表与法规/演示种子、静态托管 frontend/dist、统一错误格式）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from backend.auth import AuthUser, get_current_user, record_audit, resolve_show_demo_login
import backend.database as db_mod
from backend.database import PROJECT_ROOT, get_db, init_db
from backend.models import BidCheckTask, ensure_bid_check_tasks_columns
from backend.routers import (
    auth as auth_router_mod,
    checks as checks_router_mod,
    composer as composer_router_mod,
    dashboard as dashboard_router_mod,
    deviation as deviation_router_mod,
    extract as extract_router_mod,
    model_config as model_config_router_mod,
    qa as qa_router_mod,
    radar as radar_router_mod,
    regulations as regulations_router_mod,
    reports as reports_router_mod,
    rules as rules_router_mod,
    similarity as similarity_router_mod,
)
from backend.schemas import HealthOut
from backend.worker import _utc_now, get_worker

DIST_DIR = PROJECT_ROOT / 'frontend' / 'dist'


def recover_stale_tasks(session_factory=None) -> int:
    """E30：服务重启后将遗留处于 extracting/checking/grading 的非终态任务（含 radar）统一置 failed('服务重启中断')。"""
    sf = session_factory or db_mod.SessionLocal
    if sf is None:
        return 0
    sess: Session = sf()
    recovered_count = 0
    try:
        ensure_bid_check_tasks_columns(sess.get_bind())
        stale_rows = (
            sess.query(BidCheckTask)
            .filter(BidCheckTask.status.in_(('extracting', 'checking', 'grading')))
            .all()
        )
        if not stale_rows:
            return 0
        worker = get_worker()
        now_utc = _utc_now()
        audit_items: list[tuple[int, int, Optional[int]]] = []
        for row in stale_rows:
            row.status = 'failed'
            row.stage_detail = '服务重启中断'
            row.error_msg = '服务重启中断'
            row.finished_at = now_utc
            row.updated_at = now_utc
            worker.broadcaster.clear(row.id)
            audit_items.append((row.id, int(row.tenant_id), row.user_id))
        sess.commit()
        recovered_count = len(audit_items)
        for tid, tenant_id, uid in audit_items:
            try:
                record_audit(
                    sess,
                    action='recover_stale_tasks',
                    tenant_id=tenant_id,
                    user_id=uid,
                    target=f"task:{tid}",
                    detail_json={'reason': 'service_restart'},
                )
            except Exception:
                pass
        return recovered_count
    except Exception:
        sess.rollback()
        return 0
    finally:
        sess.close()


def _parse_cors_origins() -> list[str]:
    raw = os.environ.get('CORS_ORIGINS', 'http://localhost:5173,http://127.0.0.1:80').strip()
    origins = [x.strip() for x in raw.split(',') if x.strip()]
    return origins or ['http://localhost:5173', 'http://127.0.0.1:80']


def _should_seed_demo() -> bool:
    raw = os.environ.get('SEED_DEMO_DATA', '1').strip().lower()
    return raw not in ('0', 'false', 'no', 'off')


def create_app(
    database_url: Optional[str] = None,
    auto_init_db: bool = True,
) -> FastAPI:
    """构建并装配 FastAPI 应用实例（支持测试注入隔离数据库 URL）。"""
    if database_url:
        eng = db_mod.get_engine(database_url, ensure_dir=True)
        db_mod.engine = eng
        db_mod.SessionLocal = db_mod.get_session_factory(eng)
        get_worker(session_factory=db_mod.SessionLocal)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if auto_init_db:
            eng = init_db(database_url=database_url, seed=_should_seed_demo())
            db_mod.engine = eng
            db_mod.SessionLocal = db_mod.get_session_factory(eng)
            get_worker(session_factory=db_mod.SessionLocal)
        recover_stale_tasks(db_mod.SessionLocal)
        yield

    # ENABLE_API_DOCS：交互式 API 文档开关（沿用 Contract-Review-Agent T53 生产安全三件套惯例）。
    # FastAPI 默认自动生成 /docs（Swagger UI）、/redoc 与 /openapi.json 全端点清单——开发调试利器，
    # 但生产服务器上等于把全部接口公开发给任何人（侦察地图）。设为 0 时三处彻底关闭，业务接口不受影响。
    _api_docs_enabled = os.environ.get('ENABLE_API_DOCS', '1').strip() not in ('0', 'false', 'False')

    app = FastAPI(
        title='BidGuard-Agent · 投标方标书防废标体检与偏离表生成系统',
        version='1.0.0',
        lifespan=lifespan,
        docs_url='/docs' if _api_docs_enabled else None,
        redoc_url='/redoc' if _api_docs_enabled else None,
        openapi_url='/openapi.json' if _api_docs_enabled else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_parse_cors_origins(),
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )

    @app.exception_handler(HTTPException)
    async def unified_http_exception_handler(_request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and 'code' in detail and 'message' in detail:
            payload = {'detail': detail}
        else:
            code_map = {
                400: 'BAD_REQUEST',
                401: 'UNAUTHORIZED',
                403: 'FORBIDDEN',
                404: 'NOT_FOUND',
                422: 'VALIDATION_ERROR',
            }
            payload = {
                'detail': {
                    'code': code_map.get(exc.status_code, f"HTTP_{exc.status_code}"),
                    'message': str(detail),
                }
            }
        return JSONResponse(status_code=exc.status_code, content=payload)

    @app.exception_handler(RequestValidationError)
    async def unified_validation_exception_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                'detail': {
                    'code': 'VALIDATION_ERROR',
                    'message': f"请求参数校验失败: {exc.errors()}",
                }
            },
        )

    @app.exception_handler(Exception)
    async def unified_unhandled_exception_handler(_request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={
                'detail': {
                    'code': 'INTERNAL_ERROR',
                    'message': f"服务端内部错误: {type(exc).__name__}",
                }
            },
        )

    # ── 挂载全部业务路由（含 E4 新增 regulations 路由）────────────────────────
    app.include_router(auth_router_mod.router)
    app.include_router(dashboard_router_mod.router)
    app.include_router(checks_router_mod.router)
    app.include_router(extract_router_mod.router)
    app.include_router(deviation_router_mod.router)
    app.include_router(radar_router_mod.router)
    app.include_router(qa_router_mod.router)
    app.include_router(rules_router_mod.router)
    app.include_router(reports_router_mod.router)
    app.include_router(model_config_router_mod.router)
    app.include_router(regulations_router_mod.router)
    app.include_router(similarity_router_mod.router)
    app.include_router(composer_router_mod.router)

    @app.get('/api/health', response_model=HealthOut)
    def root_health() -> HealthOut:
        return HealthOut(
            status='ok',
            service='bidguard-agent',
            version='1.0.0',
            show_demo_login=resolve_show_demo_login(),
        )

    @app.get('/api/files/{file_path:path}')
    def download_stored_file(
        file_path: str,
        current_user: AuthUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """INTERFACE.md §3.4 统一文件下载端点（带路径穿越与租户归属双重校验）。"""
        tenant_id = int(current_user.tenant_id)
        uploads_root = (PROJECT_ROOT / 'data' / 'uploads').resolve()
        output_root = (PROJECT_ROOT / 'output').resolve()
        candidate = (PROJECT_ROOT / file_path.lstrip('/')).resolve()

        if candidate.is_relative_to(uploads_root):
            try:
                rel_parts = candidate.relative_to(uploads_root).parts
            except ValueError:
                rel_parts = ()
            if not rel_parts or str(rel_parts[0]) != str(tenant_id):
                raise HTTPException(
                    status_code=404,
                    detail={'code': 'FILE_NOT_FOUND', 'message': '请求的文件不存在或无权访问'},
                )
        elif candidate.is_relative_to(output_root):
            task_prefix = candidate.name.split('_', 1)[0]
            if not task_prefix.isdigit():
                raise HTTPException(
                    status_code=404,
                    detail={'code': 'FILE_NOT_FOUND', 'message': '请求的文件不存在或无权访问'},
                )
            owner = db.query(BidCheckTask).filter_by(id=int(task_prefix)).first()
            if owner is None or int(owner.tenant_id) != tenant_id:
                raise HTTPException(
                    status_code=404,
                    detail={'code': 'FILE_NOT_FOUND', 'message': '请求的文件不存在或无权访问'},
                )
        else:
            raise HTTPException(
                status_code=404,
                detail={'code': 'FILE_NOT_FOUND', 'message': '请求的文件不存在或无权访问'},
            )

        if not candidate.is_file():
            raise HTTPException(
                status_code=404,
                detail={'code': 'FILE_NOT_FOUND', 'message': '请求的文件不存在或无权访问'},
            )
        return FileResponse(str(candidate), filename=candidate.name)

    if DIST_DIR.is_dir() and (DIST_DIR / 'index.html').is_file():
        assets_dir = DIST_DIR / 'assets'
        if assets_dir.is_dir():
            app.mount('/assets', StaticFiles(directory=str(assets_dir)), name='assets')

        @app.get('/', include_in_schema=False)
        def serve_index():
            return FileResponse(str(DIST_DIR / 'index.html'))

        @app.get('/{spa_path:path}', include_in_schema=False)
        def spa_history_fallback(spa_path: str):
            if spa_path.startswith('api/') or spa_path.startswith('assets/'):
                raise HTTPException(
                    status_code=404,
                    detail={'code': 'NOT_FOUND', 'message': '资源不存在'},
                )
            candidate = (DIST_DIR / spa_path).resolve()
            if candidate.is_file() and candidate.is_relative_to(DIST_DIR.resolve()):
                return FileResponse(str(candidate))
            return FileResponse(str(DIST_DIR / 'index.html'))

    return app


app = create_app()