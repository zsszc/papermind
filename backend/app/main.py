from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.database import engine, Base, ensure_schema
from app.core.config import config
from app.core.logger import logger
from app.models import ensure_papers_fts
from app.services.llm import llm_service
from app.services.backup import auto_backup, cleanup_old_backups
from app.routers import papers, search, chat, thesis, memory, export, settings


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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(papers.router, prefix="/api/papers", tags=["papers"])
app.include_router(search.router, prefix="/api/search", tags=["search"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(thesis.router, prefix="/api/thesis", tags=["thesis"])
app.include_router(memory.router, prefix="/api/memory", tags=["memory"])
app.include_router(export.router, prefix="/api/export", tags=["export"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])

# 静态文件服务：用于访问 PDF 等本地资源
project_root = Path(__file__).resolve().parents[2]
app.mount("/static", StaticFiles(directory=project_root), name="static")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """捕获所有未处理异常，返回统一错误响应。"""
    logger.exception(f"[api] 未处理异常: {request.method} {request.url.path}")
    return JSONResponse(
        status_code=500,
        content={
            "detail": str(exc) or "服务器内部错误",
            "error_code": "internal_error",
            "path": request.url.path,
        },
    )


@app.get("/api/health")
async def health_check():
    return {
        "status": "ok",
        "version": config.get("app.version", "1.0.0"),
        "llm_ready": getattr(app.state, "llm_ready", False),
    }
