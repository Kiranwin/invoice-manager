"""Shared invoice and reimbursement operations for Web and MCP entry points."""

import json
from pathlib import Path

from .database import get_backend
from .db_backends import amount_cents
from .processor import InvoiceProcessor

INVOICE_STATES = {"pending", "ready", "excluded", "reported"}


def import_invoice(config: dict, path: Path, source: str = "manual") -> dict | None:
    return InvoiceProcessor(config).process_file(Path(path), source=source)


def change_invoice_state(invoice_id: int, state: str) -> None:
    if state not in INVOICE_STATES:
        raise ValueError(f"无效发票状态: {state}")
    backend = get_backend()
    invoice = backend.get_invoice_by_id(invoice_id)
    if not invoice:
        raise ValueError(f"发票 #{invoice_id} 不存在")
    if invoice["status"] == "reported" and state != "reported":
        raise ValueError("已报销发票不能直接恢复，请先核对报销记录")
    if not backend.set_invoice_state(invoice_id, state):
        raise ValueError(f"发票 #{invoice_id} 状态更新失败")


def reimbursable_invoices(filters: dict | None = None) -> list[dict]:
    return get_backend().query_invoices({**(filters or {}), "only_included": True})


def record_reimbursement(invoices: list[dict], files: list[Path], mark_reported: bool = False) -> int:
    if not invoices:
        raise ValueError("报销单不能没有发票")
    ids = [i["id"] for i in invoices]
    if len(ids) != len(set(ids)):
        raise ValueError("报销单中存在重复发票")
    if not files or any(not path.is_file() for path in files):
        raise ValueError("导出文件不完整，未记录报销单")
    return get_backend().create_reimbursement(invoices, [str(path) for path in files], mark_reported)


def list_reimbursements() -> list[dict]:
    batches = get_backend().list_reimbursements()
    for batch in batches:
        try:
            paths = json.loads(batch["output_files"])
        except (TypeError, ValueError):
            paths = []
        batch["files"] = [Path(path).name for path in paths if Path(path).is_file()]
    return batches


def record_import_failure(filename: str, path: Path, error: str) -> int:
    return get_backend().add_import_failure(filename, str(path), error)


def list_import_failures() -> list[dict]:
    return get_backend().list_import_failures()


def retry_import_failure(config: dict, failure_id: int) -> dict | None:
    backend = get_backend()
    failure = next((row for row in backend.list_import_failures() if row["id"] == failure_id), None)
    if not failure:
        raise ValueError("失败记录不存在")
    path = Path(failure["stored_path"])
    if not path.is_file():
        raise ValueError("原文件已不存在")
    processor = InvoiceProcessor(config)
    result = processor.process_file(path, source="retry")
    if result:
        backend.delete_import_failure(failure_id)
    return result


def delete_invoice_with_files(invoice_id: int) -> bool:
    backend = get_backend()
    invoice = backend.get_invoice_by_id(invoice_id)
    if not invoice:
        return False
    # Reimbursement history retains invoice references. The database foreign key
    # also protects this case when a second process writes concurrently.
    if backend.invoice_has_reimbursement(invoice_id):
        raise ValueError("发票已关联报销单，不能删除")
    paths = [Path(a["stored_path"]) for a in backend.get_attachments(invoice_id) if a.get("stored_path")]
    if invoice.get("stored_path"):
        paths.append(Path(invoice["stored_path"]))
    if not backend.delete_invoice(invoice_id):
        return False
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # The database deletion has committed. Keep the file for a later cleanup.
            pass
    return True


__all__ = [
    "INVOICE_STATES", "amount_cents", "import_invoice", "change_invoice_state",
    "reimbursable_invoices", "record_reimbursement", "list_reimbursements",
    "record_import_failure", "list_import_failures", "retry_import_failure",
    "delete_invoice_with_files",
]
