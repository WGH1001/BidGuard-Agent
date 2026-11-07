# ==============================================================================
# BidGuard-Agent · 投标方标书防废标体检与偏离表生成智能体 生产运行镜像
# 前置步骤：先在宿主机完成前端构建（cd frontend && npm run build），再执行镜像构建
# 构建命令：docker compose build 或 docker build -t bidguard-agent:1.0 .
# ==============================================================================

# 基础镜像按 digest 钉死（与老项目同款做法）：终结上游标签漂移/镜像源差异导致的构建缓存全量失效
# digest 取自本机构建实际解析版本（2026-11-06 构建日志），后续如需升级基础镜像手动更换 digest
FROM python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 1. 安装 RapidOCR (OpenCV) 所需的底层图形动态库与时区数据（切换阿里云 apt 源加速）
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g;s/security.debian.org/mirrors.aliyun.com/g' \
        /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null || true \
    && apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libxcb1 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# 2. 优先复制 requirements.txt 并安装 Python 依赖（含 psycopg2-binary 与 redis）
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# 3. 按白名单复制核心算法层、后端服务层、配置文件与宿主机已构建好的前端静态产物
COPY core/ /app/core/
COPY backend/ /app/backend/
COPY config/ /app/config/
COPY frontend/dist/ /app/frontend/dist/

# 4. 创建持久化运行目录（挂载卷：/app/data 存上传标书原件，/app/output 存偏离表与标红交付包）
RUN mkdir -p /app/data/uploads /app/output/deviation /app/output/redline /app/output/deliver
VOLUME ["/app/data", "/app/output"]

EXPOSE 8000

# 5. 容器存活与就绪探针（使用 Python 标准库 urllib 请求 /api/health）
HEALTHCHECK --interval=20s --timeout=5s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"]

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]