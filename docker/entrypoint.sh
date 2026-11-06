#!/bin/sh
# 容器入口：按 PUID/PGID 降权、首次启动生成 /data/config.yaml
set -e

CONFIG="${INVOICE_MANAGER_CONFIG:-/data/config.yaml}"
DATA_DIR="$(dirname "$CONFIG")"

mkdir -p "$DATA_DIR"

# 未指定 PUID/PGID 时，直接跟随数据目录的现有属主，也就是"不管它也能跑"。
# 目录是刚用 root 建出来的空目录（属主 0）时，回退到 1000，与镜像里的 app 用户一致。
if [ -z "${PUID:-}" ] || [ -z "${PGID:-}" ]; then
    owner_uid="$(stat -c %u "$DATA_DIR")"
    owner_gid="$(stat -c %g "$DATA_DIR")"
    if [ "$owner_uid" = "0" ]; then
        owner_uid=1000
        owner_gid=1000
    fi
    PUID="${PUID:-$owner_uid}"
    PGID="${PGID:-$owner_gid}"
fi

# 以 root 启动时，先把运行用户改成 PUID/PGID，再用同一脚本重入一次。
# 重入后 id -u 不再为 0，会直接走下面的配置生成与启动，所有文件也都是
# 目标用户建的，不会在宿主机上变成 root 所有。
if [ "$(id -u)" = "0" ]; then
    if [ "$PUID" != "0" ]; then
        if [ "$(id -g app)" != "$PGID" ]; then
            groupmod -o -g "$PGID" app
        fi
        if [ "$(id -u app)" != "$PUID" ]; then
            usermod -o -u "$PUID" app
        fi
        # 属主不一致时才递归改，避免每次启动都扫一遍整个数据卷
        if [ "$(stat -c %u "$DATA_DIR")" != "$PUID" ] || [ "$(stat -c %g "$DATA_DIR")" != "$PGID" ]; then
            echo "[entrypoint] 调整数据目录属主为 ${PUID}:${PGID}"
            chown -R "$PUID:$PGID" "$DATA_DIR"
        fi
        exec gosu app "$0" "$@"
    fi
    echo "[entrypoint] PUID=0，以 root 运行，写出的文件在宿主机上属主为 root"
fi

if [ ! -w "$DATA_DIR" ]; then
    echo "[entrypoint] 数据目录不可写：${DATA_DIR}" >&2
    echo "[entrypoint] 在 compose 里设置 PUID/PGID 为宿主机目录的属主，或调整目录权限" >&2
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
  # 容器默认只走百度 OCR：本地 PaddleOCR 依赖未安装（装了镜像会涨到 2G 以上），
  # 所以下面 text_ocr 即使改成 true 也不会生效
  text_ocr:
    enabled: false
    text_threshold: 100
    ocr_fallback: true
    pdf_dpi: 200

  # 百度增值税发票识别：容器里唯一默认启用的引擎，填 key 后重启即生效
  # 申请地址 https://console.bce.baidu.com/ai/#/ai/ocr/overview
  baidu:
    enabled: true
    api_key: ""
    secret_key: ""

  # 大模型视觉识别：容器里可用（只依赖 httpx），但默认不用。
  # 需要作为百度失败后的备用时，把 enabled 改成 true 并填下面三项
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

# 百度未配置时，识别必然全失败。这里直接看应用解析后的最终配置，
# 所以 yaml 里的值与环境变量注入的值都能识别到。
PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [ -n "$PYTHON_BIN" ]; then
    if ! "$PYTHON_BIN" - <<'PY'
import sys

try:
    from invoice_clipper import load_config
except Exception:
    # 导不进包就不判断，不误报
    sys.exit(0)

config, _ = load_config()
baidu = ((config.get("ocr") or {}).get("baidu")) or {}
ok = baidu.get("enabled") and baidu.get("api_key") and baidu.get("secret_key")
sys.exit(0 if ok else 1)
PY
    then
        echo "[entrypoint] 提醒：百度 OCR 还没有 api_key / secret_key，现在上传发票会识别失败"
        echo "[entrypoint] 两种填法任选：写进 ${CONFIG}，或用环境变量"
        echo "[entrypoint]   INVOICE_BAIDU_API_KEY / INVOICE_BAIDU_SECRET_KEY（可写在 .env 里）"
    fi
fi

exec "$@"
