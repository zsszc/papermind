"""受限静态文件服务：仅允许访问白名单目录内的文件。

替代原先对整个项目根的 StaticFiles 挂载，防止 ../ 路径穿越
以及项目根敏感文件（config.yaml、backend 源码、data/ 等）泄露。
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import config

router = APIRouter()

# 允许通过 /static 访问的一级子目录（与前端实际使用保持一致）
ALLOWED_DIRS = ("papers", "notes", "my-thesis", "summaries")


def _resolve_static_path(file_path: str) -> Path:
    """把 /static 下的相对路径解析为绝对路径，并校验其落在白名单目录内。

    - 一级目录不在白名单 -> 403
    - resolve() 后越出白名单目录（../ 穿越、软链接逃逸）-> 403
    - 目标不存在或不是文件 -> 404
    """
    parts = Path(file_path).parts
    if not parts or parts[0] not in ALLOWED_DIRS:
        raise HTTPException(status_code=403, detail="禁止访问该路径")
    allowed_root = (config.runtime_root / parts[0]).resolve()
    target = (allowed_root.joinpath(*parts[1:])).resolve() if len(parts) > 1 else allowed_root
    if target != allowed_root and allowed_root not in target.parents:
        raise HTTPException(status_code=403, detail="禁止访问该路径")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return target


@router.get("/static/{file_path:path}")
async def serve_static(file_path: str):
    """按白名单提供静态文件（PDF、笔记、概括、大论文等本地资源）。"""
    return FileResponse(_resolve_static_path(file_path))
