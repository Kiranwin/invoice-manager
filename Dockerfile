# syntax=docker/dockerfile:1
#
# 发票夹子容器镜像
#   - 只装核心依赖（不含 ofd / pg / ocr 可选组），体积约 300MB
#   - 非 root 运行，唯一持久化目录 /data
#   - 首次启动自动生成 /data/config.yaml
#
FROM python:3.12-slim

ARG TZ=Asia/Shanghai
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=${TZ} \
    INVOICE_MANAGER_CONFIG=/data/config.yaml

WORKDIR /app

# 时区数据用于本地时间显示；PyMuPDF 自带 wheel，不需要额外的系统库
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# 先拷元数据与包源码再安装，改代码时能复用依赖层缓存
COPY pyproject.toml README.md ./
COPY invoice_clipper ./invoice_clipper
RUN pip install --no-cache-dir .

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data \
    && chown -R app:app /app /data

USER app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/inbox', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# 直接用 uvicorn 起，跳过 __run__.py 的进程锁与自动开浏览器（容器里都不需要）
CMD ["uvicorn", "invoice_clipper.web:app", "--host", "0.0.0.0", "--port", "8000"]
