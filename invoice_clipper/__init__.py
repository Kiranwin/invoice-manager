"""
发票夹子核心模块 - v3.6.2
提供发票处理、数据库操作、导出功能的统一入口
"""
__version__ = "3.6.2"

import logging
import os
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

from .processor import InvoiceProcessor
from .database import (
    init_db, set_backend, get_backend,
    insert_invoice,
    query_invoices,
    query_invoices_page,
    invoice_stats,
    update_invoice,
    get_invoice_by_id,
    get_all_invoices,
    get_projects,
    add_project,
    delete_project,
    get_persons,
    add_person,
    delete_person,
    is_duplicate,
    exists_by_invoice_number,
    get_attachments,
    insert_attachment,
    delete_attachment,
    delete_invoice,
    get_tags,
    add_tag,
    delete_tag,
    get_invoice_tags,
    set_invoice_tags,
    get_invoices_by_ids,
    get_all_invoice_tags,
)
from .db_backends import DatabaseBackend, SQLiteBackend
from .exporter import export_excel, export_merged_pdf, build_export_label, export_zip_sources, export_bundle, resolve_export_dir
from .matcher import find_best_match, find_multiple_candidates
from .file_utils import (
    ofd_to_pdf, extract_text_from_pdf,
    build_archive_path, archive_invoice,
    build_attachment_path, next_attachment_seq,
)


# ── 安装目录解析 ──────────────────────────────────


def get_invoice_root() -> str:
    """确定发票夹子的根目录。

    优先级：
    1. INVOICE_ROOT 环境变量
    2. 当前工作目录（CWD）
    """
    if path := os.environ.get("INVOICE_ROOT"):
        return os.path.realpath(path)
    return os.path.realpath(os.getcwd())


# ── 默认配置 ──────────────────────────────────────


DEFAULT_CONFIG = {
    "storage": {
        "base_dir": "",          # 运行时由 init 或 get_invoice_root 填充
        "db_type": "sqlite",
        "db_path": "",           # 运行时填充
    },
    "server": {
        "port": 8000,
        "host": "127.0.0.1",
    },
    "mcp": {
        "transport": "http",      # http (Streamable HTTP) | sse | stdio
    },
    "watch_dirs": [],
    "ocr": {
        "text_ocr": {
            "enabled": False,
        },
        "baidu": {
            "enabled": False,
        },
        "llm": {
            "enabled": False,
        },
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个字典。override 优先，两边同为字典时逐层合并。"""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = val
    return result


# ── 环境变量覆盖 ────────────────────────────────

# 凭据不进配置文件：容器环境用环境变量或 docker secrets 挂文件都可以
ENV_OVERRIDES = (
    ("INVOICE_BAIDU_API_KEY", ("ocr", "baidu", "api_key")),
    ("INVOICE_BAIDU_SECRET_KEY", ("ocr", "baidu", "secret_key")),
    ("INVOICE_LLM_API_KEY", ("ocr", "llm", "api_key")),
    ("INVOICE_LLM_BASE_URL", ("ocr", "llm", "base_url")),
    ("INVOICE_LLM_MODEL", ("ocr", "llm", "model")),
)


def apply_env_overrides(config: dict, environ: dict | None = None) -> dict:
    """用环境变量覆盖配置里的凭据类字段，返回同一个 dict。

    支持两种写法，优先级从高到低：
      INVOICE_BAIDU_API_KEY=xxx
      INVOICE_BAIDU_API_KEY_FILE=/run/secrets/baidu_key
    值为空字符串时视为未设置，不覆盖配置文件里的已有值。
    """
    env = os.environ if environ is None else environ
    for env_name, path in ENV_OVERRIDES:
        value = (env.get(env_name) or "").strip()
        if not value:
            file_path = (env.get(f"{env_name}_FILE") or "").strip()
            if not file_path:
                continue
            try:
                value = Path(file_path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                logger.warning("读取 %s 指向的文件失败: %s", env_name, exc)
                continue
            if not value:
                continue

        node = config
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return config


# ── 配置加载 ──────────────────────────────────────


def load_config() -> dict:
    """加载配置，与 DEFAULT_CONFIG 合并以保证字段完整性。

    搜索顺序（先到先用）：
    0. INVOICE_MANAGER_CONFIG 环境变量
    1. {INVOICE_ROOT}/config/config.yaml（根目录下的配置）
    2. ~/.config/invoice-manager/config.yaml（XDG fallback）
    3. 包相对路径 config/config.yaml（开发模式）

    返回值：(merged_config_dict, config_path)
    """
    import shutil

    root = get_invoice_root()
    candidates = []

    # 0. 环境变量
    if env_cfg := os.environ.get("INVOICE_MANAGER_CONFIG"):
        candidates.append(Path(env_cfg))

    # 1. INVOICE_ROOT 下的配置
    candidates.append(Path(root) / "config" / "config.yaml")

    # 2. XDG fallback
    candidates.append(Path.home() / ".config" / "invoice-manager" / "config.yaml")

    # 3. 开发模式 / 打包内
    candidates.append(Path(__file__).resolve().parent.parent / "config" / "config.yaml")

    user_cfg = {}
    loaded_path = None
    for cfg in candidates:
        if cfg.exists():
            with open(cfg, encoding="utf-8") as f:
                user_cfg = yaml.safe_load(f) or {}
            loaded_path = cfg
            break

    if loaded_path is None:
        # ── 首次运行：自动初始化 ──────────────────────
        pkg_cfg_example = Path(__file__).parent / "config.example.yaml"
        if not pkg_cfg_example.exists():
            raise FileNotFoundError(
                "未找到内置示例配置，无法自动初始化。请手动创建 config/config.yaml。\n"
                "已查找:\n  " + "\n  ".join(str(c) for c in candidates)
            )

        target = Path(root) / "config" / "config.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(pkg_cfg_example), str(target))
        loaded_path = target
        print(f"📄 已创建默认配置文件: {target}")

        # 创建数据目录
        (Path(root) / "data" / "inbox").mkdir(parents=True, exist_ok=True)

        # 初始化数据库
        from .database import init_db as _init_db
        db_path = str(Path(root) / "data" / "invoices.db")
        _init_db({"storage": {"db_type": "sqlite", "db_path": db_path}})
        print(f"🗄️  已初始化数据库: {db_path}")

        # 创建安装标记
        (Path(root) / ".invoice-install").write_text(
            f"# invoice-manager 自动初始化\n# {Path(root)}\n"
        )

        with open(target, encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}

    # 用 root 填充默认值
    merged = deep_merge(DEFAULT_CONFIG, user_cfg)
    if not merged["storage"]["base_dir"]:
        merged["storage"]["base_dir"] = root
    if not merged["storage"]["db_path"]:
        merged["storage"]["db_path"] = str(Path(root) / "data" / "invoices.db")

    # 环境变量优先级高于配置文件，用于容器里注入凭据
    apply_env_overrides(merged)

    return merged, loaded_path


# ── 模块导出 ──────────────────────────────────────

__all__ = [
    # 根目录
    "get_invoice_root",
    # 配置
    "load_config",
    "DEFAULT_CONFIG",
    "deep_merge",
    "apply_env_overrides",
    # 处理器
    "InvoiceProcessor",
    # 数据库核心
    "init_db", "set_backend", "get_backend",
    "insert_invoice", "query_invoices",
    "query_invoices_page", "invoice_stats",
    "update_invoice", "get_invoice_by_id", "get_all_invoices",
    "is_duplicate", "exists_by_invoice_number",
    # 后端类
    "DatabaseBackend", "SQLiteBackend",
    # 归属管理
    "get_projects", "add_project", "delete_project",
    "get_persons", "add_person", "delete_person",
    # 附件
    "get_attachments", "insert_attachment",
    "delete_attachment",
    "delete_invoice",
    # 标签系统
    "get_tags", "add_tag", "delete_tag",
    "get_invoice_tags", "set_invoice_tags",
    "get_invoices_by_ids", "get_all_invoice_tags",
    # 导出
    "export_excel", "export_merged_pdf", "build_export_label", "export_zip_sources", "export_bundle",
    "resolve_export_dir",
    # 凑票
    "find_best_match", "find_multiple_candidates",
    # 文件工具
    "ofd_to_pdf", "extract_text_from_pdf",
    "build_archive_path", "archive_invoice",
    "build_attachment_path", "next_attachment_seq",
]
