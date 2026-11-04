"""
发票数据库模块 — 调度层

通过全局 _backend 实例将操作委托给实际的后端实现（SQLite / PostgreSQL）。
对外函数签名与 v2.0 保持兼容。
"""
import logging
from typing import Optional, List
from .db_backends import SQLiteBackend, PostgreSQLBackend, DatabaseBackend

logger = logging.getLogger(__name__)

# ── 全局后端 ──────────────────────────────────────

_backend: Optional[DatabaseBackend] = None
_initialized_signature: Optional[str] = None


def get_backend() -> DatabaseBackend:
    """获取当前全局后端实例"""
    if _backend is None:
        raise RuntimeError(
            "数据库后端未初始化。请先调用 init_db(config) 或 set_backend(config)。"
        )
    return _backend


def set_backend(config: dict) -> DatabaseBackend:
    """根据配置创建并设置全局后端"""
    global _backend
    storage = config if isinstance(config, dict) and "db_type" in config else config.get("storage", config)
    db_type = storage.get("db_type", "sqlite")
    if db_type == "postgresql":
        _backend = PostgreSQLBackend(config)
        logger.info("数据库后端: PostgreSQL")
    else:
        _backend = SQLiteBackend(config)
        logger.info("数据库后端: SQLite")
    return _backend


def _signature(db_path_or_config) -> str:
    """用于判断这套配置是否已经建过表，避免每次新建处理器都重跑 DDL。"""
    if isinstance(db_path_or_config, str):
        return f"sqlite:{db_path_or_config}"
    storage = (db_path_or_config or {}).get("storage", db_path_or_config or {})
    if storage.get("db_type") == "postgresql":
        keys = ("pg_host", "pg_port", "pg_database", "pg_user")
        return "pg:" + ":".join(str(storage.get(k, "")) for k in keys)
    return f"sqlite:{storage.get('db_path', '')}"


# ── 初始化 ────────────────────────────────────────


def init_db(db_path_or_config):
    """
    初始化数据库。

    Args:
        db_path_or_config: 可以是 SQLite 路径字符串（向后兼容），
                          也可以是配置字典（包含 storage.db_type 等字段）。

    同一进程内对同一套配置重复调用时只建表一次（DDL 幂等但开销不小）。
    """
    global _backend, _initialized_signature
    signature = _signature(db_path_or_config)
    if _backend is not None and _initialized_signature == signature:
        return
    if isinstance(db_path_or_config, str):
        # 向后兼容：传入的是 SQLite 路径
        backend = SQLiteBackend({"storage": {"db_type": "sqlite", "db_path": db_path_or_config}})
        _backend = backend
    else:
        set_backend(db_path_or_config)
    _initialized_signature = None  # 换后端后必须重新建表
    get_backend().init_db()
    _initialized_signature = signature
    logger.info("数据库初始化完成")


def get_conn(db_path: str = None):
    """
    获取数据库连接（向后兼容）。
    如果 _backend 已设置，直接返回其后端连接；
    否则根据 db_path 创建临时 SQLiteBackend。
    """
    if _backend is not None:
        return _backend.get_conn()
    # 向后兼容：直接创建 SQLite 连接
    backend = SQLiteBackend({"storage": {"db_path": db_path or ":memory:"}})
    return backend.get_conn()


# ── 发票 CRUD ─────────────────────────────────────


def insert_invoice(data: dict) -> int:
    return get_backend().insert_invoice(data)


def is_duplicate(invoice_number: str, amount_with_tax: float) -> bool:
    return get_backend().is_duplicate(invoice_number, amount_with_tax)


def exists_by_invoice_number(invoice_number: str) -> bool:
    return get_backend().exists_by_invoice_number(invoice_number)


def query_invoices(filters: dict) -> List[dict]:
    return get_backend().query_invoices(filters)


def query_invoices_page(filters: dict, order_by: str = "invoice_date",
                        order_dir: str = "desc", page: int = 1,
                        per_page: int = 50):
    """分页查询，返回 (当页记录, 总条数, 实际页码, 总页数)。"""
    return get_backend().query_invoices_page(filters, order_by, order_dir, page, per_page)


def invoice_stats() -> dict:
    """按状态聚合的发票统计。"""
    return get_backend().invoice_stats()


def update_invoice(inv_id: int, updates: dict):
    get_backend().update_invoice(inv_id, updates)


def get_invoice_by_id(invoice_id: int) -> Optional[dict]:
    return get_backend().get_invoice_by_id(invoice_id)


def get_all_invoices() -> List[dict]:
    return get_backend().get_all_invoices()


# ── 归属管理 CRUD ────────────────────────────────


def get_projects() -> List[dict]:
    """获取所有归属项目 [{id, name, created_at}]"""
    return get_backend().get_projects()


def add_project(name: str) -> int:
    """创建归属项目，返回 id"""
    return get_backend().add_project(name)


def delete_project(project_id: int) -> bool:
    """删除归属项目（无发票引用时）"""
    return get_backend().delete_project(project_id)


def get_persons() -> List[dict]:
    """获取所有归属人 [{id, name, created_at}]"""
    return get_backend().get_persons()


def add_person(name: str) -> int:
    """创建归属人，返回 id"""
    return get_backend().add_person(name)


def delete_person(person_id: int) -> bool:
    """删除归属人（无发票引用时）"""
    return get_backend().delete_person(person_id)


# ── 附件 CRUD ─────────────────────────────────────


def get_attachments(invoice_id: int) -> List[dict]:
    return get_backend().get_attachments(invoice_id)


def insert_attachment(attachment: dict) -> int:
    return get_backend().insert_attachment(attachment)


def delete_attachment(attachment_id: int) -> bool:
    return get_backend().delete_attachment(attachment_id)


def delete_invoice(invoice_id: int) -> bool:
    return get_backend().delete_invoice(invoice_id)


# ── 标签系统 ─────────────────────────────────────


def get_tags() -> List[dict]:
    """获取所有标签 [{id, name, color, created_at}]"""
    return get_backend().get_tags()


def add_tag(name: str, color: str = "#3b82f6") -> int:
    """创建标签，返回 id"""
    return get_backend().add_tag(name, color)


def delete_tag(tag_id: int) -> bool:
    """删除标签"""
    return get_backend().delete_tag(tag_id)


def get_invoice_tags(invoice_id: int) -> List[dict]:
    """获取发票的所有标签"""
    return get_backend().get_invoice_tags(invoice_id)


def set_invoice_tags(invoice_id: int, tag_ids: List[int]):
    """设置发票的标签（全量替换）"""
    get_backend().set_invoice_tags(invoice_id, tag_ids)


def get_invoices_by_ids(ids: List[int]) -> List[dict]:
    """根据 ID 列表批量获取发票"""
    return get_backend().get_invoices_by_ids(ids)


def get_all_invoice_tags() -> dict:
    """返回 {invoice_id: [{id, name, color}, ...]} 映射"""
    return get_backend().get_all_invoice_tags()
