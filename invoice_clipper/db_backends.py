"""
数据库后端抽象 — 支持 SQLite / PostgreSQL

每个后端实现 DatabaseBackend 接口，供 database.py 调度层调用。
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from abc import ABC, abstractmethod
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

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


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


BATCH_STATUSES = ("exported", "reported", "rejected", "voided")


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

    # ── 报销单编辑（双方言共用实现）──────────────────
    #
    # 下面这些方法只在基类实现一次：SQL 统一用 `?` 占位，由 _sql() 转换到
    # 各后端方言；事务通过各后端的 _transaction() 上下文拿到游标。

    PLACEHOLDER = "?"

    def _sql(self, sql: str) -> str:
        """把中性的 `?` 占位符转换成当前后端使用的占位符。"""
        return sql if self.PLACEHOLDER == "?" else sql.replace("?", self.PLACEHOLDER)

    @contextmanager
    def _transaction(self):
        """产出一个可执行 SQL 的游标对象，正常退出时提交，异常时回滚。"""
        raise NotImplementedError

    def _batch_totals(self, cur, batch_id: int):
        """在事务内重算报销单的张数与合计（单位：分）。"""
        cur.execute(self._sql(
            "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) "
            "FROM reimbursement_items WHERE batch_id=?"), [batch_id])
        row = cur.fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    # ── 发票查询（双方言共用实现）──────────────────

    INVOICE_ORDER_FIELDS = {"invoice_date", "amount_with_tax", "seller_name",
                            "belong_project", "status", "created_at", "id"}

    def _invoice_where(self, filters: dict):
        """根据筛选条件拼接 WHERE 子句，返回 (子句, 参数列表)。"""
        clauses: List[str] = []
        params: List = []

        if filters.get("date_from"):
            clauses.append("invoice_date >= ?")
            params.append(filters["date_from"])
        if filters.get("date_to"):
            clauses.append("invoice_date <= ?")
            params.append(filters["date_to"])
        if filters.get("seller"):
            clauses.append("seller_name LIKE ?")
            params.append(f"%{filters['seller']}%")
        if filters.get("buyer"):
            clauses.append("buyer_name LIKE ?")
            params.append(f"%{filters['buyer']}%")
        if filters.get("project"):
            clauses.append("belong_project = ?")
            params.append(filters["project"])
        if filters.get("person"):
            clauses.append("belong_person = ?")
            params.append(filters["person"])
        if filters.get("search"):
            like = f"%{filters['search']}%"
            clauses.append("(seller_name LIKE ? OR buyer_name LIKE ? "
                           "OR invoice_number LIKE ? OR commodity_name LIKE ?)")
            params.extend([like] * 4)
        # 金额按分传入，比较时留一个极小容差，避开 REAL 存储的二进制误差
        if filters.get("amount_min_cents") is not None:
            clauses.append("amount_with_tax >= ?")
            params.append(filters["amount_min_cents"] / 100 - 1e-9)
        if filters.get("amount_max_cents") is not None:
            clauses.append("amount_with_tax <= ?")
            params.append(filters["amount_max_cents"] / 100 + 1e-9)
        statuses = [s for s in (filters.get("statuses") or []) if s]
        if statuses:
            clauses.append("status IN (" + ",".join("?" * len(statuses)) + ")")
            params.extend(statuses)
        if filters.get("tag_id"):
            clauses.append("id IN (SELECT invoice_id FROM invoice_tags WHERE tag_id=?)")
            params.append(int(filters["tag_id"]))
        if filters.get("only_included", True):
            clauses.append("status = 'ready'")
        if filters.get("exclude_ids"):
            ids = list(filters["exclude_ids"])
            clauses.append("id NOT IN (" + ",".join("?" * len(ids)) + ")")
            params.extend(ids)

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def query_invoices(self, filters: dict) -> List[dict]:
        where, params = self._invoice_where(filters)
        return self._fetchall(self._sql(
            f"SELECT * FROM invoices{where} ORDER BY invoice_date ASC, id ASC"), params)

    def query_invoices_page(self, filters: dict, order_by: str = "invoice_date",
                            order_dir: str = "desc", page: int = 1,
                            per_page: int = 50):
        """分页查询，返回 (当页记录, 总条数, 实际页码, 总页数)。

        页码超出范围时自动回退到最后一页，避免用户改完筛选后停在空白页。
        """
        where, params = self._invoice_where(filters)
        column = order_by if order_by in self.INVOICE_ORDER_FIELDS else "invoice_date"
        direction = "DESC" if str(order_dir).lower() == "desc" else "ASC"
        per_page = max(1, min(int(per_page), 500))
        page = max(1, int(page))

        total_row = self._fetchone(
            self._sql(f"SELECT COUNT(*) AS n FROM invoices{where}"), params)
        total = int((total_row or {}).get("n") or 0)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)

        rows = self._fetchall(self._sql(
            f"SELECT * FROM invoices{where} ORDER BY {column} {direction}, id {direction} "
            f"LIMIT ? OFFSET ?"), [*params, per_page, (page - 1) * per_page])
        return rows, total, page, total_pages

    def invoice_stats(self) -> dict:
        """按状态聚合的发票统计，避免把全表读进 Python。"""
        rows = self._fetchall(self._sql(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(amount_with_tax), 0) AS amount "
            "FROM invoices GROUP BY status"))
        counts = {"pending": 0, "ready": 0, "excluded": 0, "reported": 0}
        total = 0
        total_amount = 0.0
        reimbursable_amount = 0.0
        for row in rows:
            count = int(row["n"] or 0)
            amount = float(row["amount"] or 0)
            status = row["status"]
            if status in counts:
                counts[status] = count
            if status == "ready":
                reimbursable_amount += amount
            total += count
            total_amount += amount
        return {
            "total": total,
            "pending_count": counts["pending"],
            "ok_count": counts["ready"],
            "excluded_count": counts["excluded"],
            "reported_count": counts["reported"],
            "total_amount": total_amount,
            "reimbursable_amount": reimbursable_amount,
        }

    def purge_import_failures(self, older_than_days: int = 30) -> List[dict]:
        """删除过期的识别失败记录，返回被删掉的记录（调用方负责清理文件）。"""
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat(timespec="seconds")
        rows = self._fetchall(
            self._sql("SELECT * FROM import_failures WHERE created_at < ?"), [cutoff])
        if not rows:
            return []
        with self._transaction() as cur:
            for row in rows:
                cur.execute(self._sql("DELETE FROM import_failures WHERE id=?"), [row["id"]])
        return rows

    def get_reimbursement(self, batch_id: int) -> Optional[dict]:
        """返回报销单及其发票明细、事件记录。"""
        batch = self._fetchone(
            self._sql("SELECT * FROM reimbursement_batches WHERE id=?"), [batch_id])
        if not batch:
            return None
        batch["items"] = self._fetchall(self._sql(
            "SELECT i.*, ri.amount_cents AS item_amount_cents "
            "FROM reimbursement_items ri JOIN invoices i ON i.id = ri.invoice_id "
            "WHERE ri.batch_id=? ORDER BY i.invoice_date ASC, i.id ASC"), [batch_id])
        batch["events"] = self._fetchall(self._sql(
            "SELECT * FROM reimbursement_events WHERE batch_id=? ORDER BY id DESC"), [batch_id])
        return batch

    def find_batch_for_invoice(self, invoice_id: int) -> Optional[int]:
        """发票最近一次被纳入的报销单号。"""
        row = self._fetchone(self._sql(
            "SELECT batch_id FROM reimbursement_items WHERE invoice_id=? "
            "ORDER BY batch_id DESC LIMIT 1"), [invoice_id])
        return row["batch_id"] if row else None

    def invoices_in_open_batches(self, invoice_ids: List[int]) -> dict:
        """返回 {invoice_id: [batch_id]}，只统计未作废的报销单。"""
        ids = list(dict.fromkeys(int(i) for i in invoice_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self._fetchall(self._sql(
            "SELECT ri.invoice_id, b.id AS batch_id FROM reimbursement_items ri "
            "JOIN reimbursement_batches b ON b.id = ri.batch_id "
            f"WHERE b.status != 'voided' AND ri.invoice_id IN ({placeholders})"), ids)
        conflicts: dict = {}
        for row in rows:
            conflicts.setdefault(row["invoice_id"], []).append(row["batch_id"])
        return conflicts

    def add_reimbursement_event(self, batch_id: int, action: str, detail: str = ""):
        with self._transaction() as cur:
            cur.execute(self._sql(
                "INSERT INTO reimbursement_events (batch_id, created_at, action, detail) "
                "VALUES (?, ?, ?, ?)"), [batch_id, _now(), action, detail])

    def update_reimbursement_fields(self, batch_id: int, fields: dict) -> bool:
        """更新报销单的非明细字段（备注、状态、输出文件、导出模式）。"""
        allowed = {"note", "status", "output_files", "export_mode", "package_mode"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return False
        if "status" in updates and updates["status"] not in BATCH_STATUSES:
            raise ValueError(f"无效报销单状态: {updates['status']}")
        if "output_files" in updates and not isinstance(updates["output_files"], str):
            updates["output_files"] = json.dumps(updates["output_files"], ensure_ascii=False)
        updates["updated_at"] = _now()
        assignments = ", ".join(f"{key}=?" for key in updates)
        return self._execute_update(
            self._sql(f"UPDATE reimbursement_batches SET {assignments} WHERE id=?"),
            [*updates.values(), batch_id]) > 0

    def remove_reimbursement_items(self, batch_id: int, invoice_ids: List[int],
                                   target_state: str = "ready") -> dict:
        """把发票移出报销单，并把它们放回可报销池或标记为已排除。

        这一步是「已报销」发票唯一的合法回退通道：明细、发票状态、合计金额
        和事件记录在同一个事务里完成，不允许出现报销单与发票状态不一致。
        """
        if target_state not in ("ready", "excluded"):
            raise ValueError(f"无效的发票去向: {target_state}")
        ids = list(dict.fromkeys(int(i) for i in invoice_ids))
        if not ids:
            raise ValueError("请选择要移出的发票")
        placeholders = ",".join("?" * len(ids))
        now = _now()
        with self._transaction() as cur:
            cur.execute(self._sql(
                "SELECT i.id, i.invoice_number, i.seller_name, i.amount_with_tax "
                "FROM reimbursement_items ri JOIN invoices i ON i.id = ri.invoice_id "
                f"WHERE ri.batch_id=? AND ri.invoice_id IN ({placeholders})"), [batch_id, *ids])
            rows = cur.fetchall()
            found = {row[0] for row in rows}
            missing = [i for i in ids if i not in found]
            if missing:
                raise ValueError("发票不在该报销单中: " + "、".join(f"#{i}" for i in missing))
            cur.execute(self._sql("SELECT status FROM reimbursement_batches WHERE id=?"), [batch_id])
            batch_row = cur.fetchone()
            if batch_row is None:
                raise ValueError(f"报销单 #{batch_id} 不存在")
            cur.execute(self._sql(
                f"DELETE FROM reimbursement_items WHERE batch_id=? AND invoice_id IN ({placeholders})"),
                [batch_id, *ids])
            cur.execute(self._sql(
                f"UPDATE invoices SET status=?, excluded=? WHERE id IN ({placeholders})"),
                [target_state, int(target_state != "ready"), *ids])
            count, total = self._batch_totals(cur, batch_id)
            status = "voided" if count == 0 else batch_row[0]
            cur.execute(self._sql(
                "UPDATE reimbursement_batches SET total_cents=?, status=?, updated_at=? WHERE id=?"),
                [total, status, now, batch_id])
            target_label = "恢复为可报销" if target_state == "ready" else "标记为已排除"
            for row in rows:
                number = row[1] or "无号码"
                seller = row[2] or "未知销售方"
                amount = float(row[3] or 0)
                cur.execute(self._sql(
                    "INSERT INTO reimbursement_events (batch_id, created_at, action, detail) "
                    "VALUES (?, ?, ?, ?)"),
                    [batch_id, now, "removed",
                     f"移出 {number}（{seller} ¥{amount:.2f}）→ {target_label}"])
        return {"removed": len(rows), "item_count": count,
                "total_cents": total, "status": status}

    def add_reimbursement_items(self, batch_id: int, invoice_ids: List[int],
                                mark_reported: bool = False) -> dict:
        """把可报销发票补充进已有报销单。"""
        ids = list(dict.fromkeys(int(i) for i in invoice_ids))
        if not ids:
            raise ValueError("请选择要加入的发票")
        placeholders = ",".join("?" * len(ids))
        rows = self._fetchall(self._sql(
            "SELECT id, invoice_number, seller_name, amount_with_tax, status "
            f"FROM invoices WHERE id IN ({placeholders})"), [*ids])
        found = {row["id"]: row for row in rows}
        missing = [i for i in ids if i not in found]
        if missing:
            raise ValueError("发票不存在: " + "、".join(f"#{i}" for i in missing))
        unusable = [i for i in ids if found[i]["status"] != "ready"]
        if unusable:
            raise ValueError("只有可报销状态的发票能加入报销单: "
                             + "、".join(f"#{i}" for i in unusable))
        cents = {i: amount_cents(found[i]["amount_with_tax"]) for i in ids}
        now = _now()
        with self._transaction() as cur:
            cur.execute(self._sql("SELECT status FROM reimbursement_batches WHERE id=?"), [batch_id])
            batch_row = cur.fetchone()
            if batch_row is None:
                raise ValueError(f"报销单 #{batch_id} 不存在")
            batch_status = batch_row[0]
            if batch_status == "voided":
                raise ValueError("报销单已作废，请新建报销单")
            cur.execute(self._sql("SELECT invoice_id FROM reimbursement_items WHERE batch_id=?"),
                        [batch_id])
            existing = {row[0] for row in cur.fetchall()}
            dup = [i for i in ids if i in existing]
            if dup:
                raise ValueError("发票已在该报销单中: " + "、".join(f"#{i}" for i in dup))
            mark = bool(mark_reported or batch_status == "reported")
            for inv_id in ids:
                cur.execute(self._sql(
                    "INSERT INTO reimbursement_items (batch_id, invoice_id, amount_cents) "
                    "VALUES (?, ?, ?)"), [batch_id, inv_id, cents[inv_id]])
            if mark:
                cur.execute(self._sql(
                    f"UPDATE invoices SET status='reported', excluded=1 WHERE id IN ({placeholders})"),
                    [*ids])
            count, total = self._batch_totals(cur, batch_id)
            cur.execute(self._sql(
                "UPDATE reimbursement_batches SET total_cents=?, updated_at=? WHERE id=?"),
                [total, now, batch_id])
            for inv_id in ids:
                number = found[inv_id]["invoice_number"] or "无号码"
                seller = found[inv_id]["seller_name"] or "未知销售方"
                amount = float(found[inv_id]["amount_with_tax"] or 0)
                cur.execute(self._sql(
                    "INSERT INTO reimbursement_events (batch_id, created_at, action, detail) "
                    "VALUES (?, ?, ?, ?)"),
                    [batch_id, now, "added",
                     f"加入 {number}（{seller} ¥{amount:.2f}）"
                     + ("，并标记为已报销" if mark else "")])
        return {"added": len(ids), "item_count": count,
                "total_cents": total, "marked_reported": mark}


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

    PLACEHOLDER = "?"

    @contextmanager
    def _transaction(self):
        conn = self.get_conn()
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
            # 报销单可编辑后新增的字段：备注、修改时间、生成参数
            batch_columns = {row[1] for row in conn.execute("PRAGMA table_info(reimbursement_batches)")}
            for column, column_type in (
                ("note", "TEXT"), ("updated_at", "TEXT"),
                ("export_mode", "TEXT"), ("package_mode", "TEXT"),
            ):
                if column not in batch_columns:
                    conn.execute(f"ALTER TABLE reimbursement_batches ADD COLUMN {column} {column_type}")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reimbursement_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES reimbursement_batches(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    detail TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_reimb_event_batch ON reimbursement_events(batch_id)")
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
        return self._fetchall(
            "SELECT b.*, (SELECT COUNT(*) FROM reimbursement_items ri WHERE ri.batch_id=b.id) AS item_count "
            "FROM reimbursement_batches b ORDER BY b.id DESC")

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

    def update_invoice(self, inv_id: int, updates: dict):
        if not updates:
            return
        unknown = [key for key in updates if key not in INVOICE_COLS]
        if unknown:
            raise ValueError("不支持的发票字段: " + ", ".join(unknown))
        set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values()) + [inv_id]
        self._execute(f"UPDATE invoices SET {set_clause} WHERE id = ?", values)

    def get_invoice_by_id(self, invoice_id: int) -> Optional[dict]:
        return self._fetchone("SELECT * FROM invoices WHERE id=?", [invoice_id])

    def get_all_invoices(self) -> List[dict]:
        return self._fetchall("SELECT * FROM invoices ORDER BY invoice_date ASC")

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

    def _delete_attachments_by_invoice(self, invoice_id: int) -> int:
        return self._execute_update("DELETE FROM attachments WHERE invoice_id=?", [invoice_id])

    def delete_invoice(self, invoice_id: int) -> bool:
        self._delete_attachments_by_invoice(invoice_id)
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

    PLACEHOLDER = "%s"

    @contextmanager
    def _transaction(self):
        conn = self.get_conn()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

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
                # 报销单可编辑后新增的字段 + 事件表
                for column, column_type in (
                    ("note", "TEXT"), ("updated_at", "TEXT"),
                    ("export_mode", "TEXT"), ("package_mode", "TEXT"),
                ):
                    cur.execute(
                        f"ALTER TABLE reimbursement_batches ADD COLUMN IF NOT EXISTS {column} {column_type}")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS reimbursement_events (
                        id SERIAL PRIMARY KEY,
                        batch_id INTEGER NOT NULL REFERENCES reimbursement_batches(id) ON DELETE CASCADE,
                        created_at TEXT NOT NULL,
                        action TEXT NOT NULL,
                        detail TEXT
                    )
                """)
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_reimb_event_batch ON reimbursement_events(batch_id)
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
        return self._fetchall(
            "SELECT b.*, (SELECT COUNT(*) FROM reimbursement_items ri WHERE ri.batch_id=b.id) AS item_count "
            "FROM reimbursement_batches b ORDER BY b.id DESC")

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

    def update_invoice(self, inv_id: int, updates: dict):
        if not updates:
            return
        unknown = [key for key in updates if key not in INVOICE_COLS]
        if unknown:
            raise ValueError("不支持的发票字段: " + ", ".join(unknown))
        set_clause = ", ".join(f"{k} = %s" for k in updates.keys())
        values = list(updates.values()) + [inv_id]
        self._execute(f"UPDATE invoices SET {set_clause} WHERE id = %s", values)

    def get_invoice_by_id(self, invoice_id: int) -> Optional[dict]:
        return self._fetchone("SELECT * FROM invoices WHERE id=%s", [invoice_id])

    def get_all_invoices(self) -> List[dict]:
        return self._fetchall("SELECT * FROM invoices ORDER BY invoice_date ASC")

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

    def _delete_attachments_by_invoice(self, invoice_id: int) -> int:
        return self._execute_update("DELETE FROM attachments WHERE invoice_id=%s", [invoice_id])

    def delete_invoice(self, invoice_id: int) -> bool:
        self._delete_attachments_by_invoice(invoice_id)
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
