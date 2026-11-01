"""SlideSentinel — FastAPI application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import ALLOWED_ORIGINS, APP_DIR, DATA_DIR, ensure_dirs, settings
from .routers import demo, repairs, settings as settings_router, system, tasks
from .services import demo as demo_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("slidesentinel")

FRONTEND_DIST = APP_DIR.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()
    logger.info("data dir: %s", DATA_DIR)
    try:
        from .services.task_lifecycle import reconcile_active_tasks
        recovered=reconcile_active_tasks()
        if recovered['interrupted_task_ids']:
            logger.info('Interrupted task states recovered: %s',recovered['interrupted_task_ids'])
    except Exception as exc:
        logger.warning('Task ownership check unavailable: %s',exc)
    try:
        demo_service.build_fixture()
        logger.info("demo fixture ready")
    except Exception as exc:  # fixture failure must not stop the API
        logger.warning("demo fixture unavailable: %s", exc)
    yield


app = FastAPI(
    title="SlideSentinel API",
    description="AI PPT 多层质量审查智能体 · 4.4A",
    version=settings.version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def cache_policy(request: Request, call_next):
    """入口 HTML 不缓存，哈希命名的静态资源长缓存。

    没有这条策略时浏览器会按启发式缓存 ``index.html``，重新构建前端后
    仍可能打开旧界面（旧资源已被删除时更会出现空白页）。
    """
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/assets/"):
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif not path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        code = detail.get("code", "http_error")
        message = detail.get("message", str(detail))
        extra = detail.get("detail")
    else:
        code = "http_error"
        message = str(detail)
        extra = None
    return JSONResponse(status_code=exc.status_code,
                        content={"error": {"code": code, "message": message, "detail": extra}})


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": {
        "code": "validation_error",
        "message": "请求参数不合法。",
        "detail": exc.errors()[:8],
    }})


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception):
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": {
        "code": "internal_error",
        "message": "服务器内部错误，请查看后端日志。",
        "detail": None,
    }})


app.include_router(tasks.router)
app.include_router(demo.router)
app.include_router(settings_router.router)
app.include_router(system.router)
app.include_router(repairs.router)


if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str):
    if full_path.startswith("api/"):
        return JSONResponse(status_code=404, content={"error": {
            "code": "not_found", "message": f"接口不存在：/{full_path}"}})
    index = FRONTEND_DIST / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse(status_code=503, content={"error": {
        "code": "frontend_not_built",
        "message": "前端尚未构建。请运行 scripts/start_frontend.ps1，或执行 npm run build 后重启后端。",
    }})
