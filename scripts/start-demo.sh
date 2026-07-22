#!/bin/bash
# 启动演示环境：后端 + 前端，并可选导入示例论文
# 用法：
#   cd /Users/zc/Desktop/个人知识库
#   ./scripts/start-demo.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "[demo] 项目根目录: $PROJECT_ROOT"

# 某些运行环境会注入 PYTHONPATH，导致 backend venv 的依赖被外部包覆盖，
# 这里通过 unset 保证使用 backend/venv 自己的 Python 环境。
BACKEND_PYTHON="env -u PYTHONPATH $PROJECT_ROOT/backend/venv/bin/python"

cleanup() {
    echo "[demo] 正在关闭服务..."
    if [ -n "${BACKEND_PID:-}" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
        kill "$BACKEND_PID" 2>/dev/null || true
    fi
    if [ -n "${FRONTEND_PID:-}" ] && kill -0 "$FRONTEND_PID" 2>/dev/null; then
        kill "$FRONTEND_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# 如果后端已运行则复用，否则启动
if curl -s --fail http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
    echo "[demo] 后端已在运行，复用现有服务"
    BACKEND_PID=""
else
    echo "[demo] 启动后端..."
    $BACKEND_PYTHON -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 > logs/demo-backend.log 2>&1 &
    BACKEND_PID=$!

    for i in $(seq 1 30); do
        if curl -s --fail http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
            echo "[demo] 后端已就绪 (PID: $BACKEND_PID)"
            break
        fi
        sleep 1
    done

    if [ -n "$BACKEND_PID" ] && ! kill -0 "$BACKEND_PID" 2>/dev/null; then
        echo "[demo] 后端启动失败，请查看 logs/demo-backend.log"
        exit 1
    fi
fi

# 如果前端已运行则复用，否则启动（Vite dev 默认监听 localhost，不是 127.0.0.1）
if curl -s --fail http://localhost:5173/ >/dev/null 2>&1; then
    echo "[demo] 前端已在运行，复用现有服务"
    FRONTEND_PID=""
else
    echo "[demo] 启动前端..."
    (
        cd frontend
        npm run dev > "$PROJECT_ROOT/logs/demo-frontend.log" 2>&1
    ) &
    FRONTEND_PID=$!

    for i in $(seq 1 30); do
        if curl -s --fail http://localhost:5173/ >/dev/null 2>&1; then
            echo "[demo] 前端已就绪 (PID: $FRONTEND_PID)"
            break
        fi
        sleep 1
    done

    if [ -n "$FRONTEND_PID" ] && ! kill -0 "$FRONTEND_PID" 2>/dev/null; then
        echo "[demo] 前端启动失败，请查看 logs/demo-frontend.log"
        exit 1
    fi
fi

# 如果当前库为空，自动导入示例论文
PAPER_COUNT=$(curl -s http://127.0.0.1:8000/api/papers | $BACKEND_PYTHON -c "import sys, json; print(json.load(sys.stdin).get('total', 0))")
if [ "$PAPER_COUNT" -eq 0 ]; then
    echo "[demo] 文献库为空，自动导入示例论文..."
    $BACKEND_PYTHON "$PROJECT_ROOT/scripts/seed_demo.py"
else
    echo "[demo] 当前已有 $PAPER_COUNT 篇文献，跳过示例导入"
fi

echo ""
echo "============================================"
echo "  演示环境已就绪"
echo "  前端: http://localhost:5173/"
echo "  后端: http://127.0.0.1:8000/"
echo "============================================"
echo "  按 Ctrl+C 关闭服务"
echo ""

# 保持脚本运行，直到用户中断
wait
