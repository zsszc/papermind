from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.database import engine, Base, ensure_schema
from app.core.config import config
from app.core.logger import logger
from app.models import ensure_papers_fts
from app.services.llm import llm_service
from app.services.backup import auto_backup, cleanup_old_backups
from app.routers import papers, search, chat, thesis, memory, export, settings, static
from app.core.settings import apply_env_overrides, validate_startup_config
from app.core.capability import CapabilityMiddleware


def _schedule_daily_backup():
    """启动后台线程，每天凌晨 3 点执行一次自动备份。"""
    import threading
    import time

    def run():
        while True:
            now = datetime.now()
            next_run = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            sleep_seconds = (next_run - now).total_seconds()
            time.sleep(sleep_seconds)
            try:
                auto_backup()
                cleanup_old_backups(keep=10)
            except Exception as e:
                logger.warning(f"[backup] 定时备份失败: {e}")

    t = threading.Thread(target=run, daemon=True, name="daily-backup")
    t.start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 分层配置：环境变量覆盖 + 启动校验
    apply_env_overrides(config)
    validate_startup_config(config)

    Base.metadata.create_all(bind=engine)
    ensure_schema()
    ensure_papers_fts(engine)
    logger.info("[startup] 数据库表结构检查完成")

    # LLM 服务健康检查
    llm_status = await llm_service.health_check()
    app.state.llm_ready = llm_status["ok"]
    if not llm_status["ok"]:
        logger.warning(f"[startup] LLM 服务未就绪: {llm_status['error']}")
    else:
        logger.info("[startup] LLM 服务检测通过")

    # 启动每日自动备份
    _schedule_daily_backup()
    logger.info("[startup] 每日自动备份已启动")
    yield


app = FastAPI(
    title="PaperMind API",
    description="PaperMind 本地文献知识库后端 API",
    version=config.get("app.version", "1.0.0"),
    lifespan=lifespan,
)

# 能力边界先注册、CORS 后注册，使 CORS 位于最外层；这样令牌被拒绝的 401
# 仍能被 file:// 渲染进程读取，而不是退化成不可诊断的浏览器网络错误。
app.add_middleware(CapabilityMiddleware)

# CORS 严格化：仅放行本地前端开发源，不携带凭证。
# "null" 是 Electron 生产包以 file:// 加载前端时 fetch 携带的 Origin，必须显式放行。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "null"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(papers.router, prefix="/api/papers", tags=["papers"])
app.include_router(search.router, prefix="/api/search", tags=["search"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(thesis.router, prefix="/api/thesis", tags=["thesis"])
app.include_router(memory.router, prefix="/api/memory", tags=["memory"])
app.include_router(export.router, prefix="/api/export", tags=["export"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])

# MCP Server：将文献库只读能力暴露为 MCP 工具（SSE 传输）。
# 子应用路由为 /mcp/sse（长连接）与 /mcp/messages/（消息回传）；
# 必须挂在 /static 白名单路由之前，避免被静态路由抢先匹配。
from app.services.mcp_server import get_mcp_app
app.mount("/mcp", get_mcp_app())

# 受限静态文件服务：仅放行白名单目录，防止路径穿越（见 routers/static.py）
app.include_router(static.router, tags=["static"])


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """捕获所有未处理异常：详情（含堆栈）只写日志，响应不泄露内部信息。"""
    logger.exception(f"[api] 未处理异常: {request.method} {request.url.path}")
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务器内部错误，请稍后重试",
            "error_code": "internal_error",
            "path": request.url.path,
        },
    )


@app.get("/api/health")
async def health_check():
    import os

    return {
        "status": "ok",
        "version": config.get("app.version", "1.0.0"),
        "llm_ready": getattr(app.state, "llm_ready", False),
        "instance_id": os.environ.get("PAPERMIND_INSTANCE_ID") or None,
    }
