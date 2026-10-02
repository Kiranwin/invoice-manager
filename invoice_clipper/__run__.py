#!/usr/bin/env python3
"""
发票夹子 Web UI — 启动入口（v3.4.1）

用法：
    python -m invoice_clipper.__run__            # 正常启动（带进程锁）
    python -m invoice_clipper.__run__ --debug    # 调试模式（跳过进程锁）
    python -m invoice_clipper.__run__ --skip-lock  # 同上
    python -m invoice_clipper                    # 直接启动（无锁，等同 --debug）
"""
import sys
import os
import webbrowser
from pathlib import Path


def _acquire_lock(root: str) -> bool:
    """获取进程锁"""
    import socket, json, os as _os
    lock_file = Path(root) / "invoice.lock"
    try:
        fd = _os.open(str(lock_file), _os.O_CREAT | _os.O_EXCL | _os.O_WRONLY, 0o644)
        _os.write(fd, json.dumps({"pid": _os.getpid(), "host": socket.gethostname()}).encode())
        _os.close(fd)
        return True
    except FileExistsError:
        print("  ⚠ 已有实例在运行")
        return False


def _release_lock(root: str):
    lock_file = Path(root) / "invoice.lock"
    try:
        lock_file.unlink()
    except OSError:
        pass


def main():
    # 解析参数
    skip_lock = "--debug" in sys.argv or "--skip-lock" in sys.argv

    # 加载配置
    from invoice_clipper import load_config
    cfg, cfg_path = load_config()
    root = cfg.get("storage", {}).get("base_dir", "")

    if not skip_lock:
        # 进程锁（仅非调试模式启用）
        if not _acquire_lock(root):
            print("  退出（已有实例运行）")
            sys.exit(1)
    else:
        print("  🐛 调试模式 — 跳过进程锁")

    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(cfg.get("server", {}).get("port", 8000))
    url = f"http://{host}:{port}"

    print(f"🚀 发票夹子正在启动 ...")
    print(f"   📍 Web UI:    {url}")
    print(f"   📍 MCP 接口:  {url}/mcp (Streamable HTTP)")
    print(f"   📄 {cfg_path}")

    # 后台打开浏览器
    import threading
    threading.Timer(2.5, lambda: webbrowser.open(url)).start()

    # 启动 uvicorn
    from invoice_clipper.web import app
    import uvicorn
    try:
        uvicorn.run(app, host=host, port=port, reload=False)
    finally:
        if not skip_lock:
            _release_lock(root)


if __name__ == "__main__":
    main()
