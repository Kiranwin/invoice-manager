#!/bin/sh
# 容器入口：首次启动生成 /data/config.yaml，之后每次启动都直接复用
set -e

CONFIG="${INVOICE_MANAGER_CONFIG:-/data/config.yaml}"
DATA_DIR="$(dirname "$CONFIG")"

mkdir -p "$DATA_DIR"

# 宿主机挂载目录的属主不是容器内的 uid 1000 时会写不进去，这里提前说清楚
if [ ! -w "$DATA_DIR" ]; then
    echo "[entrypoint] 数据目录不可写：${DATA_DIR}" >&2
    echo "[entrypoint] 在宿主机执行：sudo chown -R 1000:1000 <挂载目录>" >&2
    exit 1
fi

if [ ! -f "$CONFIG" ]; then
    cat > "$CONFIG" <<'YAML'
# 发票夹子容器配置（首次启动自动生成）
# 填好 OCR 的 API Key 后重启容器即可开始识别

storage:
  # 发票归档根目录，容器内固定放在数据卷里
  base_dir: /data/invoices
  # 报销单导出目录
  export_dir: /data/exports
  db_type: sqlite
  db_path: /data/invoices.db

server:
  # 容器内必须监听 0.0.0.0，否则端口映射访问不到
  host: 0.0.0.0
  port: 8000

mcp:
  transport: http

watch_dirs: []

ocr:
  # 本地 OCR 需要 paddlepaddle + paddleocr，镜像会涨到 2G 以上，容器里默认关闭
  text_ocr:
    enabled: false
    text_threshold: 100
    ocr_fallback: true
    pdf_dpi: 200
  # 百度增值税发票识别（推荐，扫描件也能认）
  baidu:
    enabled: false
    api_key: ""
    secret_key: ""
  # 大模型视觉识别（OpenAI 兼容接口）
  llm:
    enabled: false
    api_key: ""
    base_url: ""
    model: ""
    timeout: 120
YAML
    echo "[entrypoint] 已生成默认配置 ${CONFIG}"
    echo "[entrypoint] 请填入 OCR API Key 后重启容器：docker compose restart"
else
    echo "[entrypoint] 使用已有配置 ${CONFIG}"
fi

exec "$@"
