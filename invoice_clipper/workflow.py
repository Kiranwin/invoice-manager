"""Shared invoice and reimbursement operations for Web and MCP entry points."""

import json
from pathlib import Path

from .database import get_backend
from .db_backends import amount_cents
from .processor import InvoiceProcessor

INVOICE_STATES = {"pending", "ready", "excluded", "reported"}

BATCH_STATUS_LABELS = {
    "exported": "已导出",
    "reported": "已报销",
    "rejected": "财务退回",
    "voided": "已作废",
}

# 用户可以在报销单上手工切换的状态（voided 由系统按明细自动推导）
MANUAL_BATCH_STATUSES = ("exported", "reported", "rejected")


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
        raise ValueError("已报销发票不能直接改回可报销，请在所属报销单里移出这张发票")
    if not backend.set_invoice_state(invoice_id, state):
        raise ValueError(f"发票 #{invoice_id} 状态更新失败")


def reimbursable_invoices(filters: dict | None = None) -> list[dict]:
    return get_backend().query_invoices({**(filters or {}), "only_included": True})


# ── 报销单 ────────────────────────────────────────


def _output_names(raw) -> list[str]:
    try:
        paths = json.loads(raw or "[]")
    except (TypeError, ValueError):
        paths = []
    return [Path(path).name for path in paths if Path(path).is_file()]


def _decorate(batch: dict) -> dict:
    batch["files"] = _output_names(batch.get("output_files"))
    batch["item_count"] = batch.get("item_count", len(batch.get("items") or []))
    batch["status_label"] = BATCH_STATUS_LABELS.get(batch["status"], batch["status"])
    batch["total_cents"] = batch.get("total_cents") or 0
    return batch


def get_reimbursement(batch_id: int) -> dict | None:
    batch = get_backend().get_reimbursement(batch_id)
    return _decorate(batch) if batch else None


def record_reimbursement(invoices: list[dict], files: list[Path], mark_reported: bool = False,
                         export_mode: str | None = None,
                         package_mode: str | None = None) -> int:
    if not invoices:
        raise ValueError("报销单不能没有发票")
    ids = [i["id"] for i in invoices]
    if len(ids) != len(set(ids)):
        raise ValueError("报销单中存在重复发票")
    if not files or any(not path.is_file() for path in files):
        raise ValueError("导出文件不完整，未记录报销单")
    backend = get_backend()
    batch_id = backend.create_reimbursement(invoices, [str(path) for path in files], mark_reported)
    modes = {key: value for key, value in
             (("export_mode", export_mode), ("package_mode", package_mode)) if value}
    if modes:
        backend.update_reimbursement_fields(batch_id, modes)
    total = sum(amount_cents(i.get("amount_with_tax")) for i in invoices) / 100
    backend.add_reimbursement_event(
        batch_id, "created",
        f"生成报销单：{len(invoices)} 张 · ¥{total:.2f}"
        + ("，并标记为已报销" if mark_reported else ""))
    return batch_id


def list_reimbursements(status: str = "") -> list[dict]:
    batches = [_decorate(batch) for batch in get_backend().list_reimbursements()]
    if status:
        batches = [b for b in batches if b["status"] == status]
    return batches


def remove_invoices_from_reimbursement(batch_id: int, invoice_ids: list[int],
                                       target_state: str = "ready") -> dict:
    """把发票移出报销单；这是已报销发票回到可报销池的唯一入口。"""
    return get_backend().remove_reimbursement_items(batch_id, invoice_ids, target_state)


def add_invoices_to_reimbursement(batch_id: int, invoice_ids: list[int],
                                  mark_reported: bool = False) -> dict:
    return get_backend().add_reimbursement_items(batch_id, invoice_ids, mark_reported)


def set_reimbursement_status(batch_id: int, status: str) -> dict:
    """手工标记报销单状态。标记为已报销时同步把成员发票置为已报销。"""
    if status not in MANUAL_BATCH_STATUSES:
        raise ValueError(f"无效报销单状态: {status}")
    backend = get_backend()
    batch = backend.get_reimbursement(batch_id)
    if not batch:
        raise ValueError(f"报销单 #{batch_id} 不存在")
    if batch["status"] == "voided":
        raise ValueError("报销单已作废，不能修改状态")
    if batch["status"] == "reported" and status == "exported":
        raise ValueError("已标记为已报销的报销单不能回到已导出，撤销请用「整单退回」")
    backend.update_reimbursement_fields(batch_id, {"status": status})
    marked = 0
    if status == "reported":
        for item in batch["items"]:
            if item.get("status") != "reported":
                backend.set_invoice_state(item["id"], "reported")
                marked += 1
    detail = f"状态改为{BATCH_STATUS_LABELS[status]}"
    if marked:
        detail += f"，{marked} 张发票标记为已报销"
    backend.add_reimbursement_event(batch_id, "status", detail)
    return {"marked": marked}


def update_reimbursement_note(batch_id: int, note: str | None) -> None:
    backend = get_backend()
    if not backend.get_reimbursement(batch_id):
        raise ValueError(f"报销单 #{batch_id} 不存在")
    note = (note or "").strip()[:500]
    backend.update_reimbursement_fields(batch_id, {"note": note})
    backend.add_reimbursement_event(batch_id, "note", "更新备注" if note else "清空备注")


def void_reimbursement(batch_id: int, target_state: str = "ready") -> dict:
    """整单退回：成员发票全部移出并放回，报销单留在历史里标记为已作废。"""
    backend = get_backend()
    batch = backend.get_reimbursement(batch_id)
    if not batch:
        raise ValueError(f"报销单 #{batch_id} 不存在")
    if batch["status"] == "voided":
        raise ValueError("报销单已作废")
    ids = [item["id"] for item in batch["items"]]
    if not ids:
        raise ValueError("报销单里没有发票")
    if target_state not in ("ready", "excluded"):
        raise ValueError("无效的发票去向")
    result = backend.remove_reimbursement_items(batch_id, ids, target_state)
    target_label = "恢复为可报销" if target_state == "ready" else "标记为已排除"
    backend.add_reimbursement_event(
        batch_id, "void", f"整单退回 {len(ids)} 张发票 → {target_label}")
    return result


def attach_reimbursement_files(batch_id: int, files: list[Path],
                               export_mode: str | None = None,
                               package_mode: str | None = None) -> None:
    """记录一次重新生成的结果，替换报销单当前指向的文件。"""
    backend = get_backend()
    fields: dict = {"output_files": [str(path) for path in files]}
    if export_mode:
        fields["export_mode"] = export_mode
    if package_mode:
        fields["package_mode"] = package_mode
    backend.update_reimbursement_fields(batch_id, fields)
    backend.add_reimbursement_event(
        batch_id, "regenerated", f"重新生成 {len(files)} 个文件")


def batch_for_invoice(invoice_id: int) -> int | None:
    return get_backend().find_batch_for_invoice(invoice_id)


def open_batch_conflicts(invoice_ids: list[int]) -> dict[int, list[int]]:
    """导出前检查：这些发票已经在哪些未作废的报销单里。"""
    return get_backend().invoices_in_open_batches(invoice_ids)


# ── 导入失败重试 ──────────────────────────────────


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


def delete_import_failure(failure_id: int) -> bool:
    """手动忽略一条失败记录，连同留在 failed_imports 里的原文件一起清掉。"""
    backend = get_backend()
    failure = next((row for row in backend.list_import_failures() if row["id"] == failure_id), None)
    if not failure:
        return False
    backend.delete_import_failure(failure_id)
    try:
        Path(failure["stored_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    return True


def purge_import_failures(days: int = 30) -> int:
    """清理超过指定天数的失败记录与对应的遗留文件。"""
    if days < 0:
        raise ValueError("保留天数不能为负数")
    rows = get_backend().purge_import_failures(days)
    for row in rows:
        try:
            Path(row["stored_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    return len(rows)


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
    "INVOICE_STATES", "BATCH_STATUS_LABELS", "MANUAL_BATCH_STATUSES",
    "amount_cents", "import_invoice", "change_invoice_state",
    "reimbursable_invoices", "record_reimbursement", "list_reimbursements",
    "get_reimbursement", "remove_invoices_from_reimbursement",
    "add_invoices_to_reimbursement", "set_reimbursement_status",
    "update_reimbursement_note", "void_reimbursement",
    "attach_reimbursement_files", "batch_for_invoice", "open_batch_conflicts",
    "record_import_failure", "list_import_failures", "retry_import_failure",
    "delete_import_failure", "purge_import_failures",
    "delete_invoice_with_files",
]
