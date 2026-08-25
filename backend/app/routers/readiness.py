"""真实语料 Benchmark 就绪度只读端点。"""

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.core.logger import logger
from app.database import get_db
from app.schemas import BenchmarkV2ReadinessResponse
from app.services.corpus_readiness import (
    PUBLIC_READINESS_FIELDS,
    get_benchmark_v2_readiness,
    unavailable_benchmark_v2_readiness,
)


router = APIRouter()


@router.get("/benchmark-v2", response_model=BenchmarkV2ReadinessResponse)
def benchmark_v2_readiness(
    response: Response,
    db: Session = Depends(get_db),
):
    """实时只读审计；异常时返回无身份信息的失败关闭状态。"""
    response.headers["Cache-Control"] = "no-store"
    try:
        result = get_benchmark_v2_readiness(db)
    except Exception as exc:
        logger.warning(
            "[readiness] Benchmark v2 审计不可用: error_type=%s",
            type(exc).__name__,
        )
        result = unavailable_benchmark_v2_readiness()
    # 路由层再次投影白名单，防止内部审计对象或未来字段被意外序列化。
    return {field: result.get(field) for field in PUBLIC_READINESS_FIELDS}
