# PaperMind 后端一体化镜像
#
# 说明：
#   - 基于 python:3.12-slim，仅包含 FastAPI 后端（前端开发期请用 npm run dev）。
#   - 仅拷贝 backend/ 与 config.yaml.example；**不拷贝 config.yaml**（含 API Key，
#     运行时通过 `-v $(pwd)/config.yaml:/app/config.yaml:ro` 挂载，见 docs/DEPLOY.md）。
#   - 配置加载逻辑（backend/app/core/config.py）：项目根无 config.yaml 时自动回退
#     config.yaml.example，因此镜像不带密钥也能启动（LLM 功能不可用，其余正常）。
#   - 数据目录通过 VOLUME 声明，容器重建后数据不丢失；也可用 -v 绑定到宿主机目录。
#   - BGE-M3 Embedding 模型（约 2GB）在**首次启动时**在线下载，不 baked 进镜像。
#     建议挂载 HuggingFace 缓存避免重复下载：
#       -v ~/.cache/huggingface:/root/.cache/huggingface
#     国内网络可设置环境变量 HF_ENDPOINT=https://hf-mirror.com 走镜像站。

FROM python:3.12-slim

# 让 Python 输出不缓冲，日志实时可见
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先拷贝依赖清单单独安装，充分利用 Docker 构建缓存
# 注意：httpx 固定 0.27.2，与 openai 1.12 兼容，勿升级
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# 拷贝后端代码与配置模板（.dockerignore 已排除 venv、数据目录等）
COPY backend/ /app/backend/
COPY config.yaml.example /app/config.yaml.example

# 数据目录（运行时自动生成；声明为卷以便持久化）
#   data/       SQLite 数据库
#   papers/     上传的 PDF
#   notes/      Markdown 笔记
#   summaries/  AI 概括输出
#   my-thesis/  大论文 Word
#   vector_db/  ChromaDB 向量库
#   logs/       应用日志
#   backups/    每日自动备份
VOLUME ["/app/data", "/app/papers", "/app/notes", "/app/summaries", \
        "/app/my-thesis", "/app/vector_db", "/app/logs", "/app/backups"]

EXPOSE 8000

# 后端以 /app/backend 为工作目录启动（app 包位于其下）
WORKDIR /app/backend
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
