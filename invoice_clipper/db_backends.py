"""
数据库后端抽象 — 支持 SQLite / PostgreSQL

每个后端实现 DatabaseBackend 接口，供 database.py 调度层调用。
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 共享常量 ──────────────────────────────────────

INVOICE_COLS = [
    "invoice_number", "invoice_code", "invoice_date",
    "commodity_name", "specification_model",
    "buyer_name", "buyer_tax_num",
    "seller_name", "seller_tax_num",
    "tax_rate", "tax_amount", "amount_with_tax",
    "category",
    "belong_project", "belong_person", "remark",
    "source", "original_filename", "stored_path",
    "created_at", "raw_text", "raw_json",
]


def amount_cents(value) -> int:
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal("1")))


# ── 抽象基类 ──────────────────────────────────────

class DatabaseBackend(ABC):
    """数据库后端接口"""

    @abstractmethod
    def init_db(self):
        """初始化表结构"""
        ...

    @abstractmethod
    def get_conn(self):
        """返回一个数据库连接（调用方负责关闭）"""
        ...

    @abstractmethod
    def insert_invoice(self, data: dict) -> int:
        ...

    @abstractmethod
    def is_duplicate(self, invoice_number: str, amount_with_tax: float) -> bool:
        ...

    @abstractmethod
    def exists_by_invoice_number(self, invoice_number: str) -> bool:
        ...

    @abstractmethod
    def query_invoices(self, filters: dict) -> List[dict]:
        ...

    @abstractmethod
    def update_invoice_status(self, inv_id: int, excluded: bool = True):
        ...

    @abstractmethod
    def update_invoice(self, inv_id: int, updates: dict):
        ...

    @abstractmethod
    def set_invoice_state(self, inv_id: int, status: str) -> bool:
        ...

    @abstractmethod
    def create_reimbursement(self, invoices: List[dict], output_files: List[str], mark_reported: bool) -> int:
        ...

    @abstractmethod
    def list_reimbursements(self) -> List[dict]:
        ...

    @abstractmethod
    def invoice_has_reimbursement(self, inv_id: int) -> bool:
        ...

    @abstractmethod
    def add_import_failure(self, filename: str, stored_path: str, error: str) -> int:
        ...

    @abstractmethod
    def list_import_failures(self) -> List[dict]:
        ...

    @abstractmethod
    def delete_import_failure(self, failure_id: int) -> bool:
        ...

    @abstractmethod
    def get_invoice_by_id(self, invoice_id: int) -> Optional[dict]:
        ...

    @abstractmethod
    def get_all_invoices(self) -> List[dict]:
        ...

    @abstractmethod
    def get_distinct_projects(self) -> List[str]:
        ...

    @abstractmethod
    def get_distinct_persons(self) -> List[str]:
        ...

    # ── 归属管理 ──────────────────────────────────────

    @abstractmethod
    def get_projects(self) -> List[dict]:
        """获取所有归属项目 [{id, name, created_at}]"""
        ...

    @abstractmethod
    def add_project(self, name: str) -> int:
        """创建归属项目，返回 id"""
        ...

    @abstractmethod
    def delete_project(self, project_id: int) -> bool:
        """删除归属项目（无发票引用时），返回是否成功"""
        ...

    @abstractmethod
    def get_persons(self) -> List[dict]:
        """获取所有归属人 [{id, name, created_at}]"""
        ...

    @abstractmethod
    def add_person(self, name: str) -> int:
        """创建归属人，返回 id"""
        ...

    @abstractmethod
    def delete_person(self, person_id: int) -> bool:
        """删除归属人（无发票引用时），返回是否成功"""
        ...

    @abstractmethod
    def get_attachments(self, invoice_id: int) -> List[dict]:
        ...

    @abstractmethod
    def insert_attachment(self, attachment: dict) -> int:
        ...

    @abstractmethod
    def delete_attachment(self, attachment_id: int) -> bool:
        ...

    @abstractmethod
    def delete_attachments_by_invoice(self, invoice_id: int) -> int:
        ...

    @abstractmethod
    def delete_invoice(self, invoice_id: int) -> bool:
        ...

    # ── 标签系统 ──────────────────────────────────────

    @abstractmethod
    def get_tags(self) -> List[dict]:
        """获取所有标签 [{id, name, color, created_at}]"""
        ...

    @abstractmethod
    def add_tag(self, name: str, color: str = "#3b82f6") -> int:
        """创建标签，返回 id"""
        ...

    @abstractmethod
    def delete_tag(self, tag_id: int) -> bool:
        """删除标签（级联删除关联关系）"""
        ...

    @abstractmethod
    def get_invoice_tags(self, invoice_id: int) -> List[dict]:
        """获取发票的所有标签 [{id, name, color}]"""
        ...

    @abstractmethod
    def set_invoice_tags(self, invoice_id: int, tag_ids: List[int]):
        """设置发票的标签（全量替换）"""
        ...

    @abstractmethod
    def get_invoices_by_ids(self, ids: List[int]) -> List[dict]:
        """根据 ID 列表批量获取发票"""
        ...

    @abstractmethod
    def search_invoices_by_tags(self, tag_ids: List[int]) -> List[dict]:
        """根据标签 ID 列表获取所有关联的发票"""
        ...


# ── SQLite 后端 ──────────────────────────────────

class SQLiteBackend(DatabaseBackend):
    """SQLite 实现 — 沿用原 sqlite3 逻辑"""

    def __init__(self, config: dict):
        storage = config if isinstance(config, dict) and "db_path" in config else config.get("storage", config)
        self.db_path = str(Path(storage["db_path"]).expanduser().resolve())

    # ── 连接 ──────────────────────────────────────

    def get_conn(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _fetchone(self, sql: str, params: list = None):
        with self.get_conn() as conn:
            row = conn.execute(sql, params or []).fetchone()
        return dict(row) if row else None

    def _fetchall(self, sql: str, params: list = None) -> List[dict]:
        with self.get_conn() as conn:
            rows = conn.execute(sql, params or []).fetchall()
        return [dict(r) for r in rows]

    def _execute(self, sql: str, params: list = None):
        with self.get_conn() as conn:
            conn.execute(sql, params or [])
            conn.commit()

    def _execute_insert(self, sql: str, params: dict) -> int:
        with self.get_conn() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid

    def _execute_update(self, sql: str, params: list = None) -> int:
        with self.get_conn() as conn:
            cur = conn.execute(sql, params or [])
            conn.commit()
            return cur.rowcount

    # ── DDL ────────────────────────────────────────

    def init_db(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invoices (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_number TEXT,
                    invoice_code TEXT,
                    invoice_date TEXT,
                    commodity_name TEXT,
                    specification_model TEXT,
                    buyer_name TEXT,
                    buyer_tax_num TEXT,
                    seller_name TEXT,
                    seller_tax_num TEXT,
                    tax_rate REAL,
                    tax_amount REAL,
                    amount_with_tax REAL,
                    category TEXT,
                    belong_project TEXT,
                    belong_person TEXT,
                    remark TEXT,
                    source TEXT,
                    original_filename TEXT,
                    stored_path TEXT,
                    excluded INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'pending',
                    created_at TEXT,
                    raw_text TEXT,
                    raw_json TEXT
                )
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(invoices)")}
            if "status" not in columns:
                conn.execute("ALTER TABLE invoices ADD COLUMN status TEXT")
                conn.execute("UPDATE invoices SET status=CASE WHEN excluded=1 THEN 'excluded' ELSE 'ready' END")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invoice_status ON invoices(status)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reimbursement_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_cents INTEGER NOT NULL,
                    output_files TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reimbursement_items (
                    batch_id INTEGER NOT NULL REFERENCES reimbursement_batches(id) ON DELETE CASCADE,
                    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE RESTRICT,
                    amount_cents INTEGER NOT NULL,
                    PRIMARY KEY (batch_id, invoice_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS import_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    original_filename TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invoice_number ON invoices(invoice_number)")
            # Existing databases from supported releases already contain the
            # full invoice schema; migration only adds the new workflow fields.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invoice_date ON invoices(invoice_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_seller_name ON invoices(seller_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_buyer_name ON invoices(buyer_name)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_belong_project ON invoices(belong_project)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_belong_person ON invoices(belong_person)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_excluded ON invoices(excluded)")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    invoice_id INTEGER NOT NULL,
                    filename TEXT,
                    original_name TEXT,
                    file_type TEXT DEFAULT 'other',
                    stored_path TEXT,
                    file_size INTEGER DEFAULT 0,
                    created_at TEXT,
                    FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_att_invoice_id ON attachments(invoice_id)")

            # 归属管理表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS belong_projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS belong_persons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            # 标签系统
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    color TEXT DEFAULT '#3b82f6',
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS invoice_tags (
                    invoice_id INTEGER NOT NULL,
                    tag_id INTEGER NOT NULL,
                    PRIMARY KEY (invoice_id, tag_id),
                    FOREIGN KEY (invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
                    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invtag_invoice ON invoice_tags(invoice_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_invtag_tag ON invoice_tags(tag_id)")

            conn.execute("PRAGMA foreign_keys = ON")
            conn.commit()
        logger.info(f"SQLite 数据库初始化完成: {self.db_path}")

    # ── CRUD ────────────────────────────────────────

    def insert_invoice(self, data: dict) -> int:
        placeholders = ",".join(":" + c for c in INVOICE_COLS)
        return self._execute_insert(
            f"INSERT INTO invoices ({','.join(INVOICE_COLS)}, status) VALUES ({placeholders}, :status)",
            {**{c: data.get(c) for c in INVOICE_COLS}, "status": data.get("status", "pending")}
        )

    def set_invoice_state(self, inv_id: int, status: str) -> bool:
        return self._execute_update(
            "UPDATE invoices SET status=?, excluded=? WHERE id=?",
            [status, int(status != "ready"), inv_id]
        ) > 0

    def create_reimbursement(self, invoices: List[dict], output_files: List[str], mark_reported: bool) -> int:
        with self.get_conn() as conn:
            ids = [i["id"] for i in invoices]
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT id, amount_with_tax, status FROM invoices WHERE id IN ({placeholders})", ids).fetchall()
            if len(rows) != len(ids) or any(row["status"] != "ready" for row in rows):
                raise ValueError("选中的发票已变化，请刷新后重试")
            cents = {row["id"]: amount_cents(row["amount_with_tax"]) for row in rows}
            cur = conn.execute(
                "INSERT INTO reimbursement_batches (created_at, status, total_cents, output_files) VALUES (?, ?, ?, ?)",
                [datetime.now().isoformat(), "reported" if mark_reported else "exported",
                 sum(cents.values()), json.dumps(output_files, ensure_ascii=False)]
            )
            batch_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO reimbursement_items (batch_id, invoice_id, amount_cents) VALUES (?, ?, ?)",
                [(batch_id, inv_id, cents[inv_id]) for inv_id in ids]
            )
            if mark_reported:
                conn.execute(f"UPDATE invoices SET status='reported', excluded=1 WHERE id IN ({placeholders})", ids)
            conn.commit()
            return batch_id

    def list_reimbursements(self) -> List[dict]:
        return self._fetchall("SELECT * FROM reimbursement_batches ORDER BY id DESC")

    def invoice_has_reimbursement(self, inv_id: int) -> bool:
        return self._fetchone("SELECT 1 FROM reimbursement_items WHERE invoice_id=?", [inv_id]) is not None

    def add_import_failure(self, filename: str, stored_path: str, error: str) -> int:
        return self._execute_insert(
            "INSERT INTO import_failures (original_filename, stored_path, error, created_at) "
            "VALUES (:original_filename, :stored_path, :error, :created_at)",
            {"original_filename": filename, "stored_path": stored_path,
             "error": error, "created_at": datetime.now().isoformat()}
        )

    def list_import_failures(self) -> List[dict]:
        return self._fetchall("SELECT * FROM import_failures ORDER BY id DESC")

    def delete_import_failure(self, failure_id: int) -> bool:
        return self._execute_update("DELETE FROM import_failures WHERE id=?", [failure_id]) > 0

    def is_duplicate(self, invoice_number: str, amount_with_tax: float) -> bool:
        if not invoice_number:
            return False
        row = self._fetchone(
            "SELECT id FROM invoices WHERE invoice_number=? AND amount_with_tax=?",
            [invoice_number, amount_with_tax]
        )
        return row is not None

    def exists_by_invoice_number(self, invoice_number: str) -> bool:
        if not invoice_number:
            return False
        row = self._fetchone(
            "SELECT id FROM invoices WHERE invoice_number=? LIMIT 1",
            [invoice_number]
        )
        return row is not None

    def query_invoices(self, filters: dict) -> List[dict]:
        sql = "SELECT * FROM invoices WHERE 1=1"
        params = []

        if filters.get("date_from"):
            sql += " AND invoice_date >= ?"
            params.append(filters["date_from"])
        if filters.get("date_to"):
            sql += " AND invoice_date <= ?"
            params.append(filters["date_to"])
        if filters.get("seller"):
            sql += " AND seller_name LIKE ?"
            params.append(f"%{filters['seller']}%")
        if filters.get("buyer"):
            sql += " AND buyer_name LIKE ?"
            params.append(f"%{filters['buyer']}%")
        if filters.get("project"):
            sql += " AND belong_project = ?"
            params.append(filters["project"])
        if filters.get("person"):
            sql += " AND belong_person = ?"
            params.append(filters["person"])
        if filters.get("only_included", True):
            sql += " AND status = 'ready'"
        if filters.get("exclude_ids"):
            placeholders = ",".join("?" * len(filters["exclude_ids"]))
            sql += f" AND id NOT IN ({placeholders})"
            params.extend(filters["exclude_ids"])

        sql += " ORDER BY invoice_date ASC, id ASC"
        return self._fetchall(sql, params)

    def update_invoice_status(self, inv_id: int, excluded: bool = True):
        self._execute("UPDATE invoices SET excluded=?, status=? WHERE id=?",
                      [1 if excluded else 0, "excluded" if excluded else "ready", inv_id])

    def update_invoice(self, inv_id: int, updates: dict):
        if not updates:
            return
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [inv_id]
        self._execute(f"UPDATE invoices SET {set_clause} WHERE id = ?", values)

    def get_invoice_by_id(self, invoice_id: int) -> Optional[dict]:
        return self._fetchone("SELECT * FROM invoices WHERE id=?", [invoice_id])

    def get_all_invoices(self) -> List[dict]:
        return self._fetchall("SELECT * FROM invoices ORDER BY invoice_date ASC")

    def get_distinct_projects(self) -> List[str]:
        rows = self._fetchall(
            "SELECT DISTINCT belong_project FROM invoices WHERE belong_project != '' ORDER BY belong_project"
        )
        return [r["belong_project"] for r in rows]

    def get_distinct_persons(self) -> List[str]:
        rows = self._fetchall(
            "SELECT DISTINCT belong_person FROM invoices WHERE belong_person != '' ORDER BY belong_person"
        )
        return [r["belong_person"] for r in rows]

    # ── 归属管理 ──────────────────────────────────

    def get_projects(self) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM belong_projects ORDER BY name ASC"
        )

    def add_project(self, name: str) -> int:
        return self._execute_insert(
            "INSERT INTO belong_projects (name, created_at) VALUES (:name, :created_at)",
            {"name": name.strip(), "created_at": datetime.now().isoformat()}
        )

    def delete_project(self, project_id: int) -> bool:
        """先检查是否有发票引用该项目"""
        row = self._fetchone(
            "SELECT COUNT(*) AS cnt FROM invoices WHERE belong_project = (SELECT name FROM belong_projects WHERE id = ?)",
            [project_id]
        )
        if row and row["cnt"] > 0:
            return False
        return self._execute_update("DELETE FROM belong_projects WHERE id=?", [project_id]) > 0

    def get_persons(self) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM belong_persons ORDER BY name ASC"
        )

    def add_person(self, name: str) -> int:
        return self._execute_insert(
            "INSERT INTO belong_persons (name, created_at) VALUES (:name, :created_at)",
            {"name": name.strip(), "created_at": datetime.now().isoformat()}
        )

    def delete_person(self, person_id: int) -> bool:
        row = self._fetchone(
            "SELECT COUNT(*) AS cnt FROM invoices WHERE belong_person = (SELECT name FROM belong_persons WHERE id = ?)",
            [person_id]
        )
        if row and row["cnt"] > 0:
            return False
        return self._execute_update("DELETE FROM belong_persons WHERE id=?", [person_id]) > 0

    # ── 附件 ──────────────────────────────────────

    def get_attachments(self, invoice_id: int) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM attachments WHERE invoice_id=? ORDER BY created_at ASC",
            [invoice_id]
        )

    def insert_attachment(self, attachment: dict) -> int:
        cols = ["invoice_id", "filename", "original_name", "file_type",
                "stored_path", "file_size", "created_at"]
        placeholders = ",".join(":" + c for c in cols)
        return self._execute_insert(
            f"INSERT INTO attachments ({','.join(cols)}) VALUES ({placeholders})",
            {k: v for k, v in attachment.items() if k in cols}
        )

    def delete_attachment(self, attachment_id: int) -> bool:
        return self._execute_update("DELETE FROM attachments WHERE id=?", [attachment_id]) > 0

    def delete_attachments_by_invoice(self, invoice_id: int) -> int:
        return self._execute_update("DELETE FROM attachments WHERE invoice_id=?", [invoice_id])

    def delete_invoice(self, invoice_id: int) -> bool:
        self.delete_attachments_by_invoice(invoice_id)
        return self._execute_update("DELETE FROM invoices WHERE id=?", [invoice_id]) > 0

    # ── 标签系统 ──────────────────────────────────────

    def get_tags(self) -> List[dict]:
        return self._fetchall("SELECT * FROM tags ORDER BY name ASC")

    def add_tag(self, name: str, color: str = "#3b82f6") -> int:
        return self._execute_insert(
            "INSERT INTO tags (name, color, created_at) VALUES (:name, :color, :created_at)",
            {"name": name.strip(), "color": color, "created_at": datetime.now().isoformat()}
        )

    def delete_tag(self, tag_id: int) -> bool:
        # CASCADE 会自动删除 invoice_tags 中的关联记录
        return self._execute_update("DELETE FROM tags WHERE id=?", [tag_id]) > 0

    def get_invoice_tags(self, invoice_id: int) -> List[dict]:
        return self._fetchall(
            "SELECT t.id, t.name, t.color FROM tags t "
            "JOIN invoice_tags it ON t.id = it.tag_id "
            "WHERE it.invoice_id = ? ORDER BY t.name",
            [invoice_id]
        )

    def set_invoice_tags(self, invoice_id: int, tag_ids: List[int]):
        self._execute("DELETE FROM invoice_tags WHERE invoice_id=?", [invoice_id])
        for tid in tag_ids:
            self._execute(
                "INSERT OR IGNORE INTO invoice_tags (invoice_id, tag_id) VALUES (?, ?)",
                [invoice_id, tid]
            )

    def get_invoices_by_ids(self, ids: List[int]) -> List[dict]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        return self._fetchall(
            f"SELECT * FROM invoices WHERE id IN ({placeholders}) ORDER BY invoice_date ASC",
            ids
        )

    def search_invoices_by_tags(self, tag_ids: List[int]) -> List[dict]:
        if not tag_ids:
            return []
        placeholders = ",".join("?" * len(tag_ids))
        return self._fetchall(
            "SELECT DISTINCT i.* FROM invoices i "
            "JOIN invoice_tags it ON i.id = it.invoice_id "
            f"WHERE it.tag_id IN ({placeholders}) "
            "ORDER BY i.invoice_date ASC",
            tag_ids
        )

    def get_all_invoice_tags(self) -> dict:
        """返回 {invoice_id: [{id, name, color}, ...]} 映射，供列表预加载使用"""
        rows = self._fetchall(
            "SELECT it.invoice_id, t.id, t.name, t.color "
            "FROM invoice_tags it JOIN tags t ON it.tag_id = t.id "
            "ORDER BY t.name"
        )
        result: dict = {}
        for r in rows:
            inv_id = r["invoice_id"]
            if inv_id not in result:
                result[inv_id] = []
            result[inv_id].append({"id": r["id"], "name": r["name"], "color": r["color"]})
        return result


# ── PostgreSQL 后端 ──────────────────────────────


class PostgreSQLBackend(DatabaseBackend):
    """PostgreSQL 实现 — 基于 psycopg2"""

    def __init__(self, config: dict):
        storage = config if isinstance(config, dict) and "pg_host" in config else config.get("storage", config)
        self.pg_config = {
            "host": storage.get("pg_host", "localhost"),
            "port": storage.get("pg_port", 5432),
            "dbname": storage.get("pg_database", "invoice_manager"),
            "user": storage.get("pg_user", "postgres"),
            "password": storage.get("pg_password", ""),
        }

    # ── 连接 ──────────────────────────────────────

    def get_conn(self):
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(**self.pg_config)
        conn.autocommit = False
        return conn

    def _fetchone(self, sql: str, params: list = None) -> Optional[dict]:
        import psycopg2.extras
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params or [])
                row = cur.fetchone()
        return dict(row) if row else None

    def _fetchall(self, sql: str, params: list = None) -> List[dict]:
        import psycopg2.extras
        with self.get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params or [])
                rows = cur.fetchall()
        return [dict(r) for r in rows]

    def _execute(self, sql: str, params: list = None):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or [])
            conn.commit()

    def _execute_insert(self, sql: str, params: list) -> int:
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                inserted_id = cur.fetchone()[0]
                conn.commit()
                return inserted_id

    def _execute_update(self, sql: str, params: list = None) -> int:
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params or [])
                conn.commit()
                return cur.rowcount

    @staticmethod
    def _p(params: list) -> list:
        """Placeholder converter: SQLite ? → PG %s (no-op, psycopg2 uses %s style)"""
        return params

    # ── DDL ────────────────────────────────────────

    def init_db(self):
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS invoices (
                        id SERIAL PRIMARY KEY,
                        invoice_number TEXT,
                        invoice_code TEXT,
                        invoice_date TEXT,
                        commodity_name TEXT,
                        specification_model TEXT,
                        buyer_name TEXT,
                        buyer_tax_num TEXT,
                        seller_name TEXT,
                        seller_tax_num TEXT,
                        tax_rate REAL,
                        tax_amount REAL,
                        amount_with_tax REAL,
                        category TEXT,
                        belong_project TEXT,
                        belong_person TEXT,
                        remark TEXT,
                        source TEXT,
                        original_filename TEXT,
                        stored_path TEXT,
                        excluded INTEGER DEFAULT 0,
                        status TEXT DEFAULT 'pending',
                        created_at TEXT,
                        raw_text TEXT,
                        raw_json TEXT
                    )
                """)
                cur.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS status TEXT")
                cur.execute("UPDATE invoices SET status=CASE WHEN excluded=1 THEN 'excluded' ELSE 'ready' END WHERE status IS NULL")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_invoice_status ON invoices(status)")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reimbursement_batches (
                        id SERIAL PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        total_cents BIGINT NOT NULL,
                        output_files TEXT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reimbursement_items (
                        batch_id INTEGER NOT NULL REFERENCES reimbursement_batches(id) ON DELETE CASCADE,
                        invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE RESTRICT,
                        amount_cents BIGINT NOT NULL,
                        PRIMARY KEY (batch_id, invoice_id)
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS import_failures (
                        id SERIAL PRIMARY KEY,
                        original_filename TEXT NOT NULL,
                        stored_path TEXT NOT NULL,
                        error TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_invoice_number ON invoices(invoice_number)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_invoice_date ON invoices(invoice_date)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_seller_name ON invoices(seller_name)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_buyer_name ON invoices(buyer_name)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_belong_project ON invoices(belong_project)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_belong_person ON invoices(belong_person)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_excluded ON invoices(excluded)
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS attachments (
                        id SERIAL PRIMARY KEY,
                        invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                        filename TEXT,
                        original_name TEXT,
                        file_type TEXT DEFAULT 'other',
                        stored_path TEXT,
                        file_size INTEGER DEFAULT 0,
                        created_at TEXT
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_att_invoice_id ON attachments(invoice_id)
                """)

                # 归属管理表
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS belong_projects (
                        id SERIAL PRIMARY KEY,
                        name TEXT UNIQUE NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS belong_persons (
                        id SERIAL PRIMARY KEY,
                        name TEXT UNIQUE NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """)
                # 标签系统
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS tags (
                        id SERIAL PRIMARY KEY,
                        name TEXT UNIQUE NOT NULL,
                        color TEXT DEFAULT '#3b82f6',
                        created_at TEXT NOT NULL
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS invoice_tags (
                        invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
                        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
                        PRIMARY KEY (invoice_id, tag_id)
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_invtag_invoice ON invoice_tags(invoice_id)
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_invtag_tag ON invoice_tags(tag_id)
                """)

            conn.commit()
        logger.info(f"PostgreSQL 数据库初始化完成: {self.pg_config['host']}/{self.pg_config['dbname']}")

    # ── CRUD ────────────────────────────────────────

    def insert_invoice(self, data: dict) -> int:
        cols = INVOICE_COLS
        placeholders = ",".join("%s" for _ in cols + ["status"])
        names = ",".join(cols + ["status"])
        values = [data.get(c) for c in cols] + [data.get("status", "pending")]
        return self._execute_insert(
            f"INSERT INTO invoices ({names}) VALUES ({placeholders}) RETURNING id",
            values
        )

    def set_invoice_state(self, inv_id: int, status: str) -> bool:
        return self._execute_update(
            "UPDATE invoices SET status=%s, excluded=%s WHERE id=%s",
            [status, int(status != "ready"), inv_id]
        ) > 0

    def create_reimbursement(self, invoices: List[dict], output_files: List[str], mark_reported: bool) -> int:
        with self.get_conn() as conn:
            with conn.cursor() as cur:
                ids = [i["id"] for i in invoices]
                cur.execute("SELECT id, amount_with_tax, status FROM invoices WHERE id = ANY(%s) FOR UPDATE", [ids])
                rows = cur.fetchall()
                if len(rows) != len(ids) or any(row[2] != "ready" for row in rows):
                    raise ValueError("选中的发票已变化，请刷新后重试")
                cents = {row[0]: amount_cents(row[1]) for row in rows}
                cur.execute(
                    "INSERT INTO reimbursement_batches (created_at, status, total_cents, output_files) "
                    "VALUES (%s, %s, %s, %s) RETURNING id",
                    [datetime.now().isoformat(), "reported" if mark_reported else "exported",
                     sum(cents.values()), json.dumps(output_files, ensure_ascii=False)]
                )
                batch_id = cur.fetchone()[0]
                for inv_id in ids:
                    cur.execute(
                        "INSERT INTO reimbursement_items (batch_id, invoice_id, amount_cents) VALUES (%s, %s, %s)",
                        [batch_id, inv_id, cents[inv_id]]
                    )
                if mark_reported:
                    cur.execute("UPDATE invoices SET status='reported', excluded=1 WHERE id = ANY(%s)", [ids])
            conn.commit()
            return batch_id

    def list_reimbursements(self) -> List[dict]:
        return self._fetchall("SELECT * FROM reimbursement_batches ORDER BY id DESC")

    def invoice_has_reimbursement(self, inv_id: int) -> bool:
        return self._fetchone("SELECT 1 FROM reimbursement_items WHERE invoice_id=%s", [inv_id]) is not None

    def add_import_failure(self, filename: str, stored_path: str, error: str) -> int:
        return self._execute_insert(
            "INSERT INTO import_failures (original_filename, stored_path, error, created_at) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            [filename, stored_path, error, datetime.now().isoformat()]
        )

    def list_import_failures(self) -> List[dict]:
        return self._fetchall("SELECT * FROM import_failures ORDER BY id DESC")

    def delete_import_failure(self, failure_id: int) -> bool:
        return self._execute_update("DELETE FROM import_failures WHERE id=%s", [failure_id]) > 0

    def is_duplicate(self, invoice_number: str, amount_with_tax: float) -> bool:
        if not invoice_number:
            return False
        row = self._fetchone(
            "SELECT id FROM invoices WHERE invoice_number=%s AND amount_with_tax=%s",
            [invoice_number, amount_with_tax]
        )
        return row is not None

    def exists_by_invoice_number(self, invoice_number: str) -> bool:
        if not invoice_number:
            return False
        row = self._fetchone(
            "SELECT id FROM invoices WHERE invoice_number=%s LIMIT 1",
            [invoice_number]
        )
        return row is not None

    def query_invoices(self, filters: dict) -> List[dict]:
        sql = "SELECT * FROM invoices WHERE 1=1"
        params = []

        if filters.get("date_from"):
            sql += " AND invoice_date >= %s"
            params.append(filters["date_from"])
        if filters.get("date_to"):
            sql += " AND invoice_date <= %s"
            params.append(filters["date_to"])
        if filters.get("seller"):
            sql += " AND seller_name LIKE %s"
            params.append(f"%{filters['seller']}%")
        if filters.get("buyer"):
            sql += " AND buyer_name LIKE %s"
            params.append(f"%{filters['buyer']}%")
        if filters.get("project"):
            sql += " AND belong_project = %s"
            params.append(filters["project"])
        if filters.get("person"):
            sql += " AND belong_person = %s"
            params.append(filters["person"])
        if filters.get("only_included", True):
            sql += " AND status = 'ready'"
        if filters.get("exclude_ids"):
            placeholders = ",".join("%s" for _ in filters["exclude_ids"])
            sql += f" AND id NOT IN ({placeholders})"
            params.extend(filters["exclude_ids"])

        sql += " ORDER BY invoice_date ASC, id ASC"
        return self._fetchall(sql, params)

    def update_invoice_status(self, inv_id: int, excluded: bool = True):
        self._execute("UPDATE invoices SET excluded=%s, status=%s WHERE id=%s",
                      [1 if excluded else 0, "excluded" if excluded else "ready", inv_id])

    def update_invoice(self, inv_id: int, updates: dict):
        if not updates:
            return
        set_clause = ", ".join(f"{k} = %s" for k in updates.keys())
        values = list(updates.values()) + [inv_id]
        self._execute(f"UPDATE invoices SET {set_clause} WHERE id = %s", values)

    def get_invoice_by_id(self, invoice_id: int) -> Optional[dict]:
        return self._fetchone("SELECT * FROM invoices WHERE id=%s", [invoice_id])

    def get_all_invoices(self) -> List[dict]:
        return self._fetchall("SELECT * FROM invoices ORDER BY invoice_date ASC")

    def get_distinct_projects(self) -> List[str]:
        rows = self._fetchall(
            "SELECT DISTINCT belong_project FROM invoices WHERE belong_project != '' ORDER BY belong_project"
        )
        return [r["belong_project"] for r in rows]

    def get_distinct_persons(self) -> List[str]:
        rows = self._fetchall(
            "SELECT DISTINCT belong_person FROM invoices WHERE belong_person != '' ORDER BY belong_person"
        )
        return [r["belong_person"] for r in rows]

    # ── 归属管理 ──────────────────────────────────

    def get_projects(self) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM belong_projects ORDER BY name ASC"
        )

    def add_project(self, name: str) -> int:
        return self._execute_insert(
            "INSERT INTO belong_projects (name, created_at) VALUES (%s, %s) RETURNING id",
            [name.strip(), datetime.now().isoformat()]
        )

    def delete_project(self, project_id: int) -> bool:
        row = self._fetchone(
            "SELECT COUNT(*) AS cnt FROM invoices WHERE belong_project = (SELECT name FROM belong_projects WHERE id = %s)",
            [project_id]
        )
        if row and row["cnt"] > 0:
            return False
        return self._execute_update("DELETE FROM belong_projects WHERE id=%s", [project_id]) > 0

    def get_persons(self) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM belong_persons ORDER BY name ASC"
        )

    def add_person(self, name: str) -> int:
        return self._execute_insert(
            "INSERT INTO belong_persons (name, created_at) VALUES (%s, %s) RETURNING id",
            [name.strip(), datetime.now().isoformat()]
        )

    def delete_person(self, person_id: int) -> bool:
        row = self._fetchone(
            "SELECT COUNT(*) AS cnt FROM invoices WHERE belong_person = (SELECT name FROM belong_persons WHERE id = %s)",
            [person_id]
        )
        if row and row["cnt"] > 0:
            return False
        return self._execute_update("DELETE FROM belong_persons WHERE id=%s", [person_id]) > 0

    # ── 附件 ──────────────────────────────────────

    def get_attachments(self, invoice_id: int) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM attachments WHERE invoice_id=%s ORDER BY created_at ASC",
            [invoice_id]
        )

    def insert_attachment(self, attachment: dict) -> int:
        cols = ["invoice_id", "filename", "original_name", "file_type",
                "stored_path", "file_size", "created_at"]
        placeholders = ",".join("%s" for _ in cols)
        names = ",".join(cols)
        values = [attachment.get(c) for c in cols]
        return self._execute_insert(
            f"INSERT INTO attachments ({names}) VALUES ({placeholders}) RETURNING id",
            values
        )

    def delete_attachment(self, attachment_id: int) -> bool:
        return self._execute_update("DELETE FROM attachments WHERE id=%s", [attachment_id]) > 0

    def delete_attachments_by_invoice(self, invoice_id: int) -> int:
        return self._execute_update("DELETE FROM attachments WHERE invoice_id=%s", [invoice_id])

    def delete_invoice(self, invoice_id: int) -> bool:
        self.delete_attachments_by_invoice(invoice_id)
        return self._execute_update("DELETE FROM invoices WHERE id=%s", [invoice_id]) > 0

    # ── 标签系统 ──────────────────────────────────────

    def get_tags(self) -> List[dict]:
        return self._fetchall("SELECT * FROM tags ORDER BY name ASC")

    def add_tag(self, name: str, color: str = "#3b82f6") -> int:
        return self._execute_insert(
            "INSERT INTO tags (name, color, created_at) VALUES (%s, %s, %s) RETURNING id",
            [name.strip(), color, datetime.now().isoformat()]
        )

    def delete_tag(self, tag_id: int) -> bool:
        return self._execute_update("DELETE FROM tags WHERE id=%s", [tag_id]) > 0

    def get_invoice_tags(self, invoice_id: int) -> List[dict]:
        return self._fetchall(
            "SELECT t.id, t.name, t.color FROM tags t "
            "JOIN invoice_tags it ON t.id = it.tag_id "
            "WHERE it.invoice_id = %s ORDER BY t.name",
            [invoice_id]
        )

    def set_invoice_tags(self, invoice_id: int, tag_ids: List[int]):
        self._execute("DELETE FROM invoice_tags WHERE invoice_id=%s", [invoice_id])
        for tid in tag_ids:
            self._execute(
                "INSERT INTO invoice_tags (invoice_id, tag_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                [invoice_id, tid]
            )

    def get_invoices_by_ids(self, ids: List[int]) -> List[dict]:
        if not ids:
            return []
        placeholders = ",".join("%s" for _ in ids)
        return self._fetchall(
            f"SELECT * FROM invoices WHERE id IN ({placeholders}) ORDER BY invoice_date ASC",
            ids
        )

    def search_invoices_by_tags(self, tag_ids: List[int]) -> List[dict]:
        if not tag_ids:
            return []
        placeholders = ",".join("%s" for _ in tag_ids)
        return self._fetchall(
            "SELECT DISTINCT i.* FROM invoices i "
            "JOIN invoice_tags it ON i.id = it.invoice_id "
            f"WHERE it.tag_id IN ({placeholders}) "
            "ORDER BY i.invoice_date ASC",
            tag_ids
        )

    def get_all_invoice_tags(self) -> dict:
        """返回 {invoice_id: [{id, name, color}, ...]} 映射"""
        rows = self._fetchall(
            "SELECT it.invoice_id, t.id, t.name, t.color "
            "FROM invoice_tags it JOIN tags t ON it.tag_id = t.id "
            "ORDER BY t.name"
        )
        result: dict = {}
        for r in rows:
            inv_id = r["invoice_id"]
            if inv_id not in result:
                result[inv_id] = []
            result[inv_id].append({"id": r["id"], "name": r["name"], "color": r["color"]})
        return result
