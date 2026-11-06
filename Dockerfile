# syntax=docker/dockerfile:1
#
# 发票夹子容器镜像
#   - 依赖按 uv.lock 安装，镜像里的版本与本地 qa 环境一致，不会随 pip 漂移
#   - 只装核心依赖（不含 ofd / ocr 可选组），使用 slim 基础镜像
#   - 非 root 运行，唯一持久化目录 /data，首次启动生成 config.yaml
#
# 注意：--frozen 会按 uv.lock 里记录的源（国内镜像）下载；若在境外 CI 上拉取
# 偏慢，可把 uv.lock 的源换成官方 PyPI 后重新锁定一次。
#
# ── 构建阶段：把依赖装进 .venv ──
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# 钉住构建工具版本，避免 uv 自身漂移导致的 lock 行为差异
RUN pip install --no-cache-dir uv==0.11.28

WORKDIR /app

# 先只装第三方依赖：改源码不会让这一层缓存失效
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable

COPY invoice_clipper ./invoice_clipper
# --no-editable：装成完整副本，运行阶段不需要源码目录
RUN uv sync --frozen --no-dev --no-editable

# ── 运行阶段：只带走 .venv，不含 uv 与构建缓存 ──
FROM python:3.12-slim

ARG TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=${TZ} \
    INVOICE_MANAGER_CONFIG=/data/config.yaml \
    PATH="/app/.venv/bin:$PATH"

# tzdata 用于本地时间显示；gosu 用于按 PUID/PGID 降权
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata gosu \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /tmp/* /var/tmp/*

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown -R app:app /app /data

# 不写 USER：以 root 启动，由 entrypoint 按 PUID/PGID 降权
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/inbox', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "invoice_clipper.web:app", "--host", "0.0.0.0", "--port", "8000"]
