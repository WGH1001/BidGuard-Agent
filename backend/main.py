# -*- coding: utf-8 -*-
"""B2 · FastAPI 主应用装配入口（CORS、10 组路由挂载、启动建表与演示种子、静态托管 frontend/dist、统一错误格式）。

契约依据：INTERFACE.md §3.1 ~ §3.5 与 B2-backend路由层.md。
"""
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

from backend.auth import AuthUser, get_current_user
import backend.database as db_mod
from backend.database import PROJECT_ROOT, get_db, init_db
from backend.models import BidCheckTask
from backend.routers import (
    auth as auth_router_mod,
    checks as checks_router_mod,
    dashboard as dashboard_router_mod,
    deviation as deviation_router_mod,
    extract as extract_router_mod,
    model_config as model_config_router_mod,
    qa as qa_router_mod,
    radar as radar_router_mod,
    reports as reports_router_mod,
    rules as rules_router_mod,
)
from backend.schemas import HealthOut
from backend.worker import get_worker

DIST_DIR = PROJECT_ROOT / 'frontend' / 'dist'


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
        yield

    app = FastAPI(
        title='BidGuard-Agent · 投标方标书防废标体检与偏离表生成系统',
        version='1.0.0',
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_parse_cors_origins(),
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )

    # ── 全局异常处理器：统一收敛为 INTERFACE.md §3.5 格式 {"detail": {"code": ..., "message": ...}} ──
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

    # ── 挂载 10 组核心业务路由 ──────────────────────────────────────────────
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

    @app.get('/api/health', response_model=HealthOut)
    def root_health() -> HealthOut:
        return HealthOut(status='ok', service='bidguard-agent', version='1.0.0')

    @app.get('/api/files/{file_path:path}')
    def download_stored_file(
        file_path: str,
        current_user: AuthUser = Depends(get_current_user),
        db: Session = Depends(get_db),
    ):
        """INTERFACE.md §3.4 统一文件下载端点。

        限制在 data/uploads 与 output 目录内（防路径穿越），且强制租户归属校验（§3.2）：
        - data/uploads/{tenant_id}/... ：路径第一段必须等于当前用户租户 id；
        - output/{deviation|redline|deliver}/{task_id}_... ：按文件名 task_id 前缀查任务归属。
        非本租户资源一律 404（防探测，与业务路由口径一致）。
        """
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

    # ── 静态托管 frontend/dist（存在时才挂载）────────────────────────────────
    if DIST_DIR.is_dir() and (DIST_DIR / 'index.html').is_file():
        assets_dir = DIST_DIR / 'assets'
        if assets_dir.is_dir():
            app.mount('/assets', StaticFiles(directory=str(assets_dir)), name='assets')

        @app.get('/', include_in_schema=False)
        def serve_index():
            return FileResponse(str(DIST_DIR / 'index.html'))

    return app


app = create_app()