"""
发票夹子核心模块 - v3.4.1
提供发票处理、数据库操作、导出功能的统一入口
"""
import os
import sys
import yaml
from pathlib import Path

from .processor import InvoiceProcessor
from .database import (
    init_db, set_backend, get_backend,
    insert_invoice,
    query_invoices,
    update_invoice_status,
    update_invoice,
    get_invoice_by_id,
    get_all_invoices,
    get_distinct_projects,
    get_distinct_persons,
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
    delete_attachments_by_invoice,
    delete_invoice,
    get_tags,
    add_tag,
    delete_tag,
    get_invoice_tags,
    set_invoice_tags,
    get_invoices_by_ids,
    search_invoices_by_tags,
    get_all_invoice_tags,
)
from .db_backends import DatabaseBackend, SQLiteBackend, PostgreSQLBackend
from .exporter import export_excel, export_merged_pdf, build_export_label, export_zip_sources
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
    """递归合并两个字典。base 中的缺失字段由 override 补充，已存在的不覆盖。"""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = val
    return result


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
                f"未找到内置示例配置，无法自动初始化。请运行 invoice-manager init。\n"
                f"已查找:\n  " + "\n  ".join(str(c) for c in candidates)
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

    return merged, loaded_path


# ── 模块导出 ──────────────────────────────────────

__all__ = [
    # 根目录
    "get_invoice_root",
    # 配置
    "load_config",
    "DEFAULT_CONFIG",
    "deep_merge",
    # 处理器
    "InvoiceProcessor",
    # 数据库核心
    "init_db", "set_backend", "get_backend",
    "insert_invoice", "query_invoices", "update_invoice_status",
    "update_invoice", "get_invoice_by_id", "get_all_invoices",
    "get_distinct_projects", "get_distinct_persons",
    "is_duplicate", "exists_by_invoice_number",
    # 后端类
    "DatabaseBackend", "SQLiteBackend", "PostgreSQLBackend",
    # 归属管理
    "get_projects", "add_project", "delete_project",
    "get_persons", "add_person", "delete_person",
    # 附件
    "get_attachments", "insert_attachment",
    "delete_attachment", "delete_attachments_by_invoice",
    "delete_invoice",
    # 标签系统
    "get_tags", "add_tag", "delete_tag",
    "get_invoice_tags", "set_invoice_tags",
    "get_invoices_by_ids", "search_invoices_by_tags", "get_all_invoice_tags",
    # 导出
    "export_excel", "export_merged_pdf", "build_export_label", "export_zip_sources",
    # 凑票
    "find_best_match", "find_multiple_candidates",
    # 文件工具
    "ofd_to_pdf", "extract_text_from_pdf",
    "build_archive_path", "archive_invoice",
    "build_attachment_path", "next_attachment_seq",
]
