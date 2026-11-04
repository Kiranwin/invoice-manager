#!/usr/bin/env python3
"""发票夹子 Web UI — FastAPI + Jinja2 (v3.6.0)"""
import json
import logging
import re
import shutil
import webbrowser
from decimal import InvalidOperation
from urllib.parse import urlencode
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, Form, File, UploadFile, Query, HTTPException
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from invoice_clipper import (
    load_config, init_db, query_invoices, query_invoices_page, invoice_stats, update_invoice,
    get_invoice_by_id,
    get_attachments, insert_attachment, delete_attachment,
    get_projects, add_project, delete_project,
    get_persons, add_person, delete_person,
    get_tags, add_tag, delete_tag, get_invoice_tags, set_invoice_tags,
    get_all_invoice_tags, get_invoices_by_ids,
    InvoiceProcessor, build_export_label, export_bundle, resolve_export_dir,
    build_attachment_path, next_attachment_seq,
)
from contextlib import asynccontextmanager

from invoice_clipper.mcp_server import mcp as mcp_server
from invoice_clipper.workflow import (
    change_invoice_state, delete_invoice_with_files, reimbursable_invoices,
    record_reimbursement, list_reimbursements, amount_cents,
    record_import_failure, list_import_failures, retry_import_failure,
    delete_import_failure, purge_import_failures,
    BATCH_STATUS_LABELS,
    get_reimbursement, remove_invoices_from_reimbursement,
    add_invoices_to_reimbursement, set_reimbursement_status,
    update_reimbursement_note, void_reimbursement,
    attach_reimbursement_files, batch_for_invoice, open_batch_conflicts,
)

logger = logging.getLogger(__name__)

# 包内资源路径（安装后模板/静态文件在包目录内）
PKG_DIR = Path(__file__).parent

# 识别失败记录的保留天数
FAILURE_RETENTION_DAYS = 30

# 识别引擎初始化不便宜，按配置缓存处理器，避免每次上传都重建
_processor_cache: dict[str, InvoiceProcessor] = {}


def get_processor(cfg: dict) -> InvoiceProcessor:
    """复用同一套配置的 InvoiceProcessor（引擎与数据库连接不必每次上传重建）。"""
    key = json.dumps({k: cfg.get(k) for k in ("storage", "ocr")}, sort_keys=True, default=str)
    proc = _processor_cache.get(key)
    if proc is None:
        proc = InvoiceProcessor(cfg)
        _processor_cache[key] = proc
    return proc


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期：加载配置 + 启动 MCP Streamable HTTP session manager"""
    cfg, cfg_path = load_config()
    app.state.config = cfg
    app.state.config_path = cfg_path
    init_db(cfg)

    # 启动时清理过期的识别失败记录，避免 failed_imports 目录只进不出
    purged = purge_import_failures(FAILURE_RETENTION_DAYS)
    if purged:
        logger.info("清理了 %d 条超过 %d 天的识别失败记录", purged, FAILURE_RETENTION_DAYS)

    # 提前调用确保 _session_manager 已创建
    mcp_server.streamable_http_app()
    async with mcp_server._session_manager.run():
        yield  # 应用运行中


app = FastAPI(title="发票夹子", version="3.6.0", lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(PKG_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(PKG_DIR / "templates"))

# 挂载 MCP Streamable HTTP 端点（AI Agent 接口）
app.mount("/mcp", mcp_server.streamable_http_app())


# ── Helpers ───────────────────────────────────────
def flash_redirect(url: str, message: str, msg_type: str = "success") -> RedirectResponse:
    return RedirectResponse(f"{url}?message={message}&msg_type={msg_type}", status_code=303)


def get_flash(request: Request) -> dict:
    return {
        "message": request.query_params.get("message"),
        "msg_type": request.query_params.get("msg_type", "success"),
    }


INVOICE_FIELDS = [
    "invoice_number", "invoice_code", "invoice_date",
    "commodity_name", "specification_model", "category",
    "buyer_name", "buyer_tax_num", "seller_name", "seller_tax_num",
    "amount_with_tax", "tax_amount", "tax_rate",
    "belong_project", "belong_person", "remark",
]


def build_filters(request: Request) -> dict:
    f = {}
    for key in ("date_from", "date_to", "seller", "buyer", "project", "person"):
        val = request.query_params.get(key)
        if val:
            f[key] = val
    return f


STATUS_NAME_TO_STATE = {"待确认": "pending", "正常": "ready",
                        "排除": "excluded", "已报销": "reported"}
LIST_SORT_FIELDS = {"invoice_date", "amount_with_tax", "seller_name",
                    "belong_project", "status"}
LIST_PAGE_SIZES = (20, 50, 100, 200)


def parse_amount_filter(raw: str) -> tuple[int | None, int | None]:
    """把金额输入解析为 (最小分, 最大分)。

    支持 `100` / `>=100` / `<=100` / `>100` / `<100` / `100-200`，
    空输入返回 (None, None)，格式错误抛 ValueError。
    """
    text = (raw or "").replace("¥", "").replace("￥", "").replace(",", "").strip()
    if not text:
        return None, None

    def cents(part: str) -> int:
        return amount_cents(part.strip())

    if text.startswith((">=", "<=")):
        value = cents(text[2:])
        return (value, None) if text[0] == ">" else (None, value)
    if text[0] in "><":
        value = cents(text[1:])
        return (value + 1, None) if text[0] == ">" else (None, max(0, value - 1))
    if "-" in text:
        low, _, high = text.partition("-")
        low_cents, high_cents = cents(low), cents(high)
        return (min(low_cents, high_cents), max(low_cents, high_cents))
    value = cents(text)
    return value, value


# ── Attachment serving ───────────────────────────


@app.get("/attachments/{att_id}/file")
def serve_attachment(att_id: int):
    """Serve attached image/pdf file by attachment ID"""
    from invoice_clipper.database import get_backend
    backend = get_backend()
    try:
        row = backend._fetchone("SELECT * FROM attachments WHERE id=?", [att_id])
    except Exception:
        row = backend._fetchone("SELECT * FROM attachments WHERE id=%s", [att_id])
    if not row:
        raise HTTPException(404, "附件不存在")
    att = dict(row)
    filepath = Path(att["stored_path"])
    if not filepath.exists():
        raise HTTPException(404, "附件文件不存在")
    # Determine media type
    ext = filepath.suffix.lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".pdf": "application/pdf",
        ".txt": "text/plain",
    }.get(ext, "application/octet-stream")
    # inline 让浏览器直接显示图片，而非下载
    disposition = f'inline; filename="{att.get("original_name") or filepath.name}"'
    return FileResponse(filepath, media_type=media_type,
                        headers={"Content-Disposition": disposition})


# ── Routes ────────────────────────────────────────

@app.get("/")
def root():
    return RedirectResponse("/inbox", status_code=303)


def _pending_invoices() -> list[dict]:
    """Return invoices that still need a human confirmation."""
    return query_invoices({"statuses": ["pending"], "only_included": False})


def _human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1024 / 1024:.2f} MB"


def _failure_context() -> dict:
    """识别失败区的展示数据：记录列表 + 占用体积。"""
    failures = list_import_failures()
    total_bytes = 0
    for item in failures:
        try:
            path = Path(item["stored_path"])
            if path.is_file():
                total_bytes += path.stat().st_size
        except OSError:
            continue
    return {
        "failures": failures,
        "failure_bytes": total_bytes,
        "failure_size_label": _human_size(total_bytes),
        "failure_retention_days": FAILURE_RETENTION_DAYS,
    }


@app.get("/inbox")
def get_inbox(request: Request):
    """Unified inbox: upload new files and review newly recognized invoices."""
    ctx = get_flash(request)
    ctx.update({
        "page": "inbox",
        "results": None,
        "summary": None,
        "pending_invoices": _pending_invoices(),
        "projects": get_projects(),
        "persons": get_persons(),
        **_failure_context(),
    })
    return templates.TemplateResponse(request, "scan.html", ctx)


@app.post("/inbox")
async def post_scan(request: Request, files: list[UploadFile] = File(...)):
    cfg = request.app.state.config
    form = await request.form()
    belong_project = form.get("belong_project", "").strip()
    belong_person = form.get("belong_person", "").strip()

    proc = get_processor(cfg)
    results = []

    for f in files:
        suffix = Path(f.filename or "").suffix.lower()
        if suffix not in {".pdf", ".ofd", ".png", ".jpg", ".jpeg", ".bmp", ".tiff"}:
            results.append({"id": "?", "filename": f.filename or "-", "ok": False,
                            "error": "不支持的文件类型", "invoice_number": "-", "invoice_date": "-",
                            "seller_name": "-", "amount_with_tax": 0,
                            "belong_project": "", "belong_person": ""})
            continue
        failed_dir = Path(cfg["storage"]["base_dir"]).expanduser() / "failed_imports"
        failed_dir.mkdir(parents=True, exist_ok=True)
        tmp = failed_dir / f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}{suffix}"
        with open(tmp, "wb") as fp:
            shutil.copyfileobj(f.file, fp)
        r = None
        error = None
        try:
            r = proc.process_file(tmp, source="web")
            if r and (belong_project or belong_person):
                updates = {}
                if belong_project:
                    updates["belong_project"] = belong_project
                if belong_person:
                    updates["belong_person"] = belong_person
                update_invoice(r["id"], updates)
                r.update(updates)
            if r is None:
                error = proc._error or "识别失败"
                record_import_failure(f.filename or tmp.name, tmp, error)
        except Exception as exc:
            # 单个文件的意外异常不能拖垮整批上传
            logger.exception("处理文件失败: %s", f.filename)
            error = f"处理异常: {exc}"
            record_import_failure(f.filename or tmp.name, tmp, error)
        else:
            if r is not None:
                tmp.unlink(missing_ok=True)
        results.append({
            "id": r.get("id", "?") if r else "?",
            "filename": f.filename or "-",
            "ok": r is not None,
            "error": error,
            "invoice_number": r.get("invoice_number", "-") if r else "-",
            "invoice_date": r.get("invoice_date", "-") if r else "-",
            "seller_name": r.get("seller_name", "-") if r else "-",
            "amount_with_tax": r.get("amount_with_tax", 0) if r else 0,
            "belong_project": belong_project or "",
            "belong_person": belong_person or "",
        })

    ok_count = sum(1 for r in results if r["ok"])
    ctx = get_flash(request)
    ctx.update({
        "page": "inbox",
        "results": results,
        "summary": {"ok_count": ok_count, "total_count": len(results)},
        "pending_invoices": _pending_invoices(),
        "projects": get_projects(),
        "persons": get_persons(),
        **_failure_context(),
    })
    return templates.TemplateResponse(request, "scan.html", ctx)


@app.post("/inbox/failures/{failure_id}/retry")
def retry_failed_import(request: Request, failure_id: int):
    try:
        result = retry_import_failure(request.app.state.config, failure_id)
    except ValueError as exc:
        return flash_redirect("/inbox", str(exc), "warning")
    if result:
        return flash_redirect("/inbox", "重试成功，请核对发票")
    return flash_redirect("/inbox", "重试仍未识别成功，请检查识别引擎配置或文件内容", "warning")


@app.post("/inbox/failures/{failure_id}/delete")
def delete_failed_import(failure_id: int):
    """忽略一条失败记录，同时清掉 failed_imports 里的原文件。"""
    if delete_import_failure(failure_id):
        return flash_redirect("/inbox", "已删除失败记录与原文件")
    return flash_redirect("/inbox", "失败记录不存在", "warning")


@app.post("/inbox/failures/purge")
def purge_failed_imports():
    removed = purge_import_failures(FAILURE_RETENTION_DAYS)
    if not removed:
        return flash_redirect("/inbox", f"没有超过 {FAILURE_RETENTION_DAYS} 天的失败记录", "warning")
    return flash_redirect(
        "/inbox", f"已清理 {removed} 条超过 {FAILURE_RETENTION_DAYS} 天的失败记录")


@app.get("/list")
def get_list(request: Request,
             search: str = "",
             status: list[str] = Query(default=["待确认", "正常"]),
             sort: str = "invoice_date",
             dir: str = "desc",
             page: int = 1,
             per_page: int = 50):
    filters = build_filters(request)
    filters["only_included"] = False
    filters["search"] = search.strip()
    filters["statuses"] = sorted({STATUS_NAME_TO_STATE[name] for name in status
                                  if name in STATUS_NAME_TO_STATE})

    amount_filter = request.query_params.get("amount", "").strip()
    tag_id = request.query_params.get("tag_id", "")
    invalid_amount = False
    try:
        min_cents, max_cents = parse_amount_filter(amount_filter)
    except (TypeError, ValueError, InvalidOperation):
        min_cents = max_cents = None
        invalid_amount = True
    if min_cents is not None:
        filters["amount_min_cents"] = min_cents
    if max_cents is not None:
        filters["amount_max_cents"] = max_cents
    if tag_id.isdigit():
        filters["tag_id"] = int(tag_id)

    sort = sort if sort in LIST_SORT_FIELDS else "invoice_date"
    dir = "asc" if str(dir).lower() == "asc" else "desc"
    per_page = per_page if per_page in LIST_PAGE_SIZES else 50
    page = max(1, int(page or 1))

    if invalid_amount:
        invoices, total, page, total_pages = [], 0, 1, 1
    else:
        invoices, total, page, total_pages = query_invoices_page(
            filters, sort, dir, page, per_page)

    def make_url(**overrides) -> str:
        """带上当前全部筛选项重新拼一个 URL，用于排序与翻页。"""
        pairs = list(request.query_params.multi_items())
        for key, value in overrides.items():
            pairs = [(k, v) for k, v in pairs if k != key]
            if value not in (None, ""):
                pairs.append((key, str(value)))
        query = urlencode(pairs)
        return "/list" + (f"?{query}" if query else "")

    sort_links = {}
    for field in ("invoice_date", "seller_name", "amount_with_tax", "belong_project", "status"):
        active = (field == sort)
        next_dir = "asc" if (active and dir == "desc") else "desc"
        sort_links[field] = {
            "url": make_url(sort=field, dir=next_dir, page=1),
            "active": active,
            "dir": dir,
        }

    page_urls = {n: make_url(page=n) for n in range(1, total_pages + 1)} if total_pages <= 9 else {}

    ctx = get_flash(request)
    ctx.update({
        "page": "list",
        "invoices": invoices,
        "search": search,
        "status_filter": status,
        "filters": {key: request.query_params.get(key, "") for key in
                    ("date_from", "date_to", "seller", "buyer", "project", "person")},
        "amount_filter": amount_filter,
        "invalid_amount": invalid_amount,
        "tag_id": tag_id,
        "all_tags": get_tags(),
        "invoice_tags": get_all_invoice_tags(),
        "projects": get_projects(),
        "persons": get_persons(),
        "stats": invoice_stats(),
        "sort": sort,
        "dir": dir,
        "sort_links": sort_links,
        "page_no": page,
        "per_page": per_page,
        "page_sizes": LIST_PAGE_SIZES,
        "total": total,
        "total_pages": total_pages,
        "page_urls": page_urls,
        "make_page_url": make_url,
        "prev_url": make_url(page=page - 1) if page > 1 else None,
        "next_url": make_url(page=page + 1) if page < total_pages else None,
    })
    return templates.TemplateResponse(request, "list.html", ctx)


@app.get("/settings")
def get_settings(request: Request):
    ctx = get_flash(request)
    ctx.update({
        "page": "settings",
        "projects": get_projects(),
        "persons": get_persons(),
        "tags": get_tags(),
    })
    return templates.TemplateResponse(request, "settings.html", ctx)


# ── 归属管理 ────────────────────────────────────────


@app.post("/settings/projects")
def add_assignment_project(request: Request, name: str = Form(...)):
    name = name.strip()
    if not name:
        return flash_redirect("/settings", "项目名称不能为空", "warning")
    try:
        add_project(name)
        return flash_redirect("/settings", f"归属项目「{name}」已创建")
    except Exception as e:
        return flash_redirect("/settings", f"创建失败: {e}", "error")


@app.post("/settings/projects/{project_id}/delete")
def delete_assignment_project(project_id: int):
    ok = delete_project(project_id)
    if ok:
        return flash_redirect("/settings", "归属项目已删除")
    return flash_redirect("/settings", "该项目已被发票引用，无法删除", "warning")


@app.post("/settings/persons")
def add_assignment_person(request: Request, name: str = Form(...)):
    name = name.strip()
    if not name:
        return flash_redirect("/settings", "归属人名称不能为空", "warning")
    try:
        add_person(name)
        return flash_redirect("/settings", f"归属人「{name}」已创建")
    except Exception as e:
        return flash_redirect("/settings", f"创建失败: {e}", "error")


@app.post("/settings/persons/{person_id}/delete")
def delete_assignment_person(person_id: int):
    ok = delete_person(person_id)
    if ok:
        return flash_redirect("/settings", "归属人已删除")
    return flash_redirect("/settings", "该归属人已被发票引用，无法删除", "warning")


# ── 标签管理 ────────────────────────────────────────


@app.post("/settings/tags")
def add_tag_route(request: Request, name: str = Form(...), color: str = Form("#3b82f6")):
    name = name.strip()
    if not name:
        return flash_redirect("/settings", "标签名称不能为空", "warning")
    try:
        add_tag(name, color)
        return flash_redirect("/settings", f"标签「{name}」已创建")
    except Exception as e:
        return flash_redirect("/settings", f"创建失败: {e}", "error")


@app.post("/settings/tags/{tag_id}/delete")
def delete_tag_route(tag_id: int):
    ok = delete_tag(tag_id)
    if ok:
        return flash_redirect("/settings", "标签已删除")
    return flash_redirect("/settings", "删除失败", "error")


@app.post("/list/batch-toggle")
def batch_toggle(request: Request, ids: list[int] = Form(...), action: str = Query(...)):
    if action not in {"exclude", "include", "confirm"}:
        raise HTTPException(400, "无效操作")
    target = "excluded" if action == "exclude" else "ready"
    updated = 0
    for inv_id in ids:
        try:
            change_invoice_state(inv_id, target)
            updated += 1
        except ValueError as exc:
            return flash_redirect("/list", f"已更新 {updated} 张；{exc}", "warning")
    label = "已排除" if action == "exclude" else "已确认" if action == "confirm" else "已恢复"
    return flash_redirect("/list", f"批量操作完成: {updated} 张发票{label}")


@app.post("/list/batch-delete")
def batch_delete(request: Request, ids: list[int] = Form(...)):
    """批量删除发票"""
    deleted = 0
    for inv_id in ids:
        try:
            if delete_invoice_with_files(inv_id):
                deleted += 1
        except ValueError as exc:
            return flash_redirect("/list", f"已删除 {deleted} 张；{exc}", "warning")
    return flash_redirect("/list", f"批量删除完成: 已删除 {deleted} 张发票")


@app.post("/list/batch-assign")
def batch_assign(request: Request, ids: list[int] = Form(...),
                 belong_project: str = Form(default=""),
                 belong_person: str = Form(default="")):
    """批量设置归属项目/归属人"""
    updates = {}
    if belong_project:
        updates["belong_project"] = belong_project
    if belong_person:
        updates["belong_person"] = belong_person
    if not updates:
        return flash_redirect("/list", "未选择归属项目或归属人", "warning")
    for inv_id in ids:
        update_invoice(inv_id, updates)
    return flash_redirect("/list", f"批量归属完成: 已更新 {len(ids)} 张发票")


@app.post("/list/{inv_id}/tags")
def set_invoice_tags_route(request: Request, inv_id: int, tag_ids: list[int] = Form(default=[])):
    set_invoice_tags(inv_id, tag_ids)
    return flash_redirect(f"/list/{inv_id}", "标签已更新")


@app.get("/list/{inv_id}")
def get_edit(request: Request, inv_id: int):
    inv = get_invoice_by_id(inv_id)
    if not inv:
        raise HTTPException(404, "发票不存在")
    attachments = get_attachments(inv_id)
    projects = get_projects()
    persons = get_persons()
    ctx = get_flash(request)
    ctx.update({
        "page": "list",
        "invoice": dict(inv),
        "attachments": attachments,
        "projects": projects,
        "persons": persons,
        "all_tags": get_tags(),
        "invoice_tags": get_invoice_tags(inv_id),
        "reimbursement_batch": batch_for_invoice(inv_id),
        "raw_json": json.dumps(dict(inv), ensure_ascii=False, indent=2, default=str),
    })
    return templates.TemplateResponse(request, "edit.html", ctx)


@app.post("/list/{inv_id}")
async def post_edit(request: Request, inv_id: int):
    form = await request.form()
    updates = {}
    for field in INVOICE_FIELDS:
        if field in form:
            val = form[field]
            if field in ("amount_with_tax", "tax_amount", "tax_rate"):
                try:
                    updates[field] = float(val)
                except (ValueError, TypeError):
                    updates[field] = 0.0
            else:
                updates[field] = str(val)
    update_invoice(inv_id, updates)
    # Handle tags
    tag_ids_raw = form.getlist("tag_ids") if hasattr(form, "getlist") else form.get("tag_ids", [])
    if tag_ids_raw:
        if isinstance(tag_ids_raw, str):
            tag_ids_raw = [tag_ids_raw]
        tag_ids = [int(t) for t in tag_ids_raw if t]
        set_invoice_tags(inv_id, tag_ids)
    else:
        set_invoice_tags(inv_id, [])
    return flash_redirect(f"/list/{inv_id}", "保存成功")


@app.post("/list/{inv_id}/toggle")
def toggle_status(request: Request, inv_id: int):
    inv = get_invoice_by_id(inv_id)
    if not inv:
        raise HTTPException(404)
    if inv.get("status") == "reported":
        return flash_redirect(f"/list/{inv_id}", "已报销发票不能直接改为可报销", "warning")
    if inv.get("status") == "pending":
        return flash_redirect(f"/list/{inv_id}", "请先核对并确认发票", "warning")
    new_status = not inv.get("excluded")
    change_invoice_state(inv_id, "excluded" if new_status else "ready")
    label = "已排除" if new_status else "已恢复"
    return flash_redirect(f"/list/{inv_id}", f"发票 #{inv_id} {label}")


@app.post("/list/{inv_id}/confirm")
def confirm_invoice(inv_id: int):
    try:
        change_invoice_state(inv_id, "ready")
    except ValueError as exc:
        return flash_redirect(f"/list/{inv_id}", str(exc), "warning")
    return flash_redirect(f"/list/{inv_id}", "发票已确认，可用于报销")


@app.post("/list/{inv_id}/delete")
def delete_invoice_route(request: Request, inv_id: int):
    inv = get_invoice_by_id(inv_id)
    if not inv:
        raise HTTPException(404)

    try:
        delete_invoice_with_files(inv_id)
    except ValueError as exc:
        return flash_redirect(f"/list/{inv_id}", str(exc), "warning")
    return flash_redirect("/list", f"发票 #{inv_id} 已删除")


@app.post("/list/{inv_id}/attachments")
async def upload_attachments(request: Request, inv_id: int,
                              files: list[UploadFile] = File(...),
                              file_type: str = Form("other")):
    inv = get_invoice_by_id(inv_id)
    if not inv:
        raise HTTPException(404)

    inv_stored = inv.get("stored_path", "")
    if not inv_stored:
        return flash_redirect(f"/list/{inv_id}",
                              "发票未归档，无法上传附件", "warning")

    seq = next_attachment_seq(inv_stored)
    count = 0
    for uf in files:
        content = await uf.read()
        if not content:
            continue
        ext = Path(uf.filename).suffix.lower() if uf.filename else ".bin"
        dest = build_attachment_path(inv_stored, seq, ext)
        with open(dest, "wb") as fp:
            fp.write(content)
        insert_attachment({
            "invoice_id": inv_id,
            "filename": dest.name,
            "original_name": uf.filename or dest.name,
            "file_type": file_type,
            "stored_path": str(dest),
            "file_size": len(content),
            "created_at": datetime.now().isoformat(),
        })
        seq += 1
        count += 1

    return flash_redirect(f"/list/{inv_id}", f"上传 {count} 个附件")


@app.post("/list/{inv_id}/attachments/{att_id}/delete")
def delete_attachment_route(request: Request, inv_id: int, att_id: int):
    atts = get_attachments(inv_id)
    target = next((a for a in atts if a["id"] == att_id), None)
    if target:
        p = Path(target["stored_path"])
        if p.exists():
            p.unlink()
        delete_attachment(att_id)
    return flash_redirect(f"/list/{inv_id}", "附件已删除")


# ── 导出辅助 ──────────────────────────────────


def build_export_files(cfg: dict, invoices: list, label: str,
                       export_mode: str, package_mode: str) -> list[dict]:
    """生成一套导出文件。首次导出与报销单重新生成共用这条路径。"""
    attachments_map = None
    if export_mode == "with_attachments":
        attachments_map = {inv["id"]: get_attachments(inv["id"]) for inv in invoices}
    return export_bundle(invoices, resolve_export_dir(cfg), label, export_mode,
                         package_mode, attachments_map)


@app.get("/export")
def get_export(request: Request):
    ctx = get_flash(request)

    # Handle pre-selected invoice IDs from list page
    selected_ids_param = request.query_params.get("ids", "")
    selected_invoices = []
    invalid_selection = False
    if selected_ids_param:
        ids_list = [int(x) for x in selected_ids_param.split(",") if x.strip().isdigit()]
        if ids_list:
            selected_invoices = [i for i in get_invoices_by_ids(ids_list) if i.get("status") == "ready"]
            invalid_selection = len(selected_invoices) != len(set(ids_list))
            selected_ids_param = ",".join(str(i["id"]) for i in selected_invoices)
        else:
            invalid_selection = True
    if invalid_selection:
        ctx.update({"message": "部分发票不可报销，请重新从发票库选择", "msg_type": "warning"})

    # Handle required invoice numbers for smart match (manually added)
    required_numbers_param = request.query_params.get("required_numbers", "")
    required_invoices: list[dict] = []
    invalid_required = False
    if required_numbers_param:
        nums_list = [n.strip() for n in required_numbers_param.split(",") if n.strip()]
        if nums_list:
            nums_set = set(nums_list)
            pool = reimbursable_invoices()
            required_invoices = [i for i in pool if i.get("invoice_number") and i["invoice_number"] in nums_set]
            invalid_required = len(required_invoices) != len(nums_set)
            required_numbers_param = ",".join(i["invoice_number"] for i in required_invoices)
        else:
            invalid_required = True
    if invalid_required:
        ctx.update({"message": "部分必选发票号码不可用，请重新从发票库选择", "msg_type": "warning"})

    ctx.update({
        "page": "export",
        "filters": {"date_from": "", "date_to": "", "seller": "", "buyer": "",
                     "project": "", "person": ""},
        "download_links": None, "invoice_count": None, "total_amount": 0.0,
        "selected_invoices": selected_invoices,
        "selected_ids": selected_ids_param,
        "required_invoices": required_invoices,
        "required_numbers": required_numbers_param,
        "required_numbers_set": {i["invoice_number"] for i in required_invoices},
        "history": list_reimbursements(),
        "target_amount": "",
        "max_count": 0,
        "candidates": None,
        "match_filters": {},
    })
    return templates.TemplateResponse(request, "export.html", ctx)


@app.post("/export")
async def post_export(request: Request):
    cfg = request.app.state.config
    form = await request.form()

    # Determine invoice selection mode
    selected_ids_str = form.get("selected_ids", "").strip()
    filters = {}
    if selected_ids_str:
        # Selected-invoice mode: get by IDs
        ids_list = [int(x) for x in selected_ids_str.split(",") if x.strip().isdigit()]
        invoices = get_invoices_by_ids(ids_list)
        if len(invoices) != len(set(ids_list)) or any(i.get("status") != "ready" for i in invoices):
            return flash_redirect("/export", "选中的发票包含待确认、已排除或已报销项", "warning")
    else:
        # Filter mode (existing): build filters from form
        for key in ("date_from", "date_to", "seller", "buyer", "project", "person"):
            val = form.get(key, "").strip()
            if val:
                filters[key] = val
        filters["only_included"] = True
        invoices = reimbursable_invoices(filters)

    total_amount = sum(amount_cents(i.get("amount_with_tax")) for i in invoices) / 100
    if not invoices:
        return flash_redirect("/export", "没有符合条件的发票", "warning")

    # Export mode
    export_mode = form.get("export_mode", "invoice_only")  # invoice_only | with_attachments
    package_mode = form.get("package_mode", "merged_pdf")  # merged_pdf | source_zip | both

    label = build_export_label(form) if not selected_ids_str else f"选中{len(invoices)}张"
    label = f"{label}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

    try:
        links = build_export_files(cfg, invoices, label, export_mode, package_mode)
    except Exception as e:
        return flash_redirect("/export", f"导出失败: {e}", "error")
    download_links = [{"label": link["label"], "filename": link["filename"]} for link in links]

    mark_reported = _truthy(form.get("mark_reported"))
    try:
        batch_id = record_reimbursement(
            invoices, [link["path"] for link in links],
            mark_reported=mark_reported,
            export_mode=export_mode, package_mode=package_mode,
        )
    except ValueError as exc:
        return flash_redirect("/export", str(exc), "warning")
    marked_count = len(invoices) if mark_reported else 0

    # 提醒已出现在其它未作废报销单里的发票，避免同一张票重复提交
    conflicts = open_batch_conflicts([i["id"] for i in invoices])
    duplicate_batches = sorted({b for batch_ids in conflicts.values() for b in batch_ids
                                if b != batch_id})

    ctx = get_flash(request)
    ctx.update({
        "page": "export",
        "filters": filters if not selected_ids_str else {},
        "download_links": download_links,
        "invoice_count": len(invoices),
        "total_amount": total_amount,
        "selected_ids": selected_ids_str,
        "selected_invoices": invoices if selected_ids_str else [],
        "required_invoices": [],
        "required_numbers": "",
        "required_numbers_set": set(),
        "marked_count": marked_count,
        "batch_id": batch_id,
        "duplicate_invoice_count": len(conflicts),
        "duplicate_batches": duplicate_batches,
        "history": list_reimbursements(),
        "target_amount": "",
        "max_count": 0,
        "candidates": None,
        "match_filters": {},
    })
    return templates.TemplateResponse(request, "export.html", ctx)


@app.get("/export/download/{filename}")
def download_export(request: Request, filename: str):
    export_dir = resolve_export_dir(request.app.state.config)
    filepath = export_dir / filename
    if not filepath.resolve().is_relative_to(export_dir.resolve()):
        raise HTTPException(404)
    if not re.match(r'^(报销(明细|发票)|源文件|发票附件)_.+\.(xlsx|pdf|zip)$', filename):
        raise HTTPException(404)
    if not filepath.exists():
        raise HTTPException(404)
    return FileResponse(filepath, filename=filename)


# ── 报销单记录与修改 ─────────────────────────────


def _int_values(values) -> list[int]:
    return [int(v) for v in values if str(v).strip().isdigit()]


def _truthy(value) -> bool:
    """表单里未勾选的复选框不会提交；提交了也可能带任意值，所以按关键字判断。"""
    return str(value if value is not None else "").strip().lower() in {"1", "on", "true", "yes"}


@app.get("/reimbursements")
def get_reimbursements(request: Request, status: str = ""):
    ctx = get_flash(request)
    batches = list_reimbursements()
    counts = {key: sum(1 for b in batches if b["status"] == key) for key in BATCH_STATUS_LABELS}
    if status in BATCH_STATUS_LABELS:
        batches = [b for b in batches if b["status"] == status]
    ctx.update({
        "page": "reimbursements",
        "batches": batches,
        "status_filter": status,
        "counts": counts,
        "total_count": sum(counts.values()),
    })
    return templates.TemplateResponse(request, "reimbursements.html", ctx)


@app.get("/reimbursements/{batch_id}")
def get_reimbursement_detail(request: Request, batch_id: int):
    batch = get_reimbursement(batch_id)
    if not batch:
        raise HTTPException(404, "报销单不存在")
    member_ids = {item["id"] for item in batch["items"]}
    available = [i for i in reimbursable_invoices() if i["id"] not in member_ids]
    available.sort(key=lambda i: i.get("invoice_date") or "", reverse=True)
    ctx = get_flash(request)
    ctx.update({
        "page": "reimbursements",
        "batch": batch,
        "items": batch["items"],
        "events": batch["events"],
        "available": available,
    })
    return templates.TemplateResponse(request, "reimbursement.html", ctx)


@app.post("/reimbursements/{batch_id}/remove")
async def post_remove_reimbursement_items(request: Request, batch_id: int):
    form = await request.form()
    ids = _int_values(form.getlist("ids"))
    target_state = form.get("target_state", "ready")
    if target_state not in ("ready", "excluded"):
        target_state = "ready"
    if not ids:
        return flash_redirect(f"/reimbursements/{batch_id}", "请先勾选要移出的发票", "warning")
    try:
        result = remove_invoices_from_reimbursement(batch_id, ids, target_state)
    except ValueError as exc:
        return flash_redirect(f"/reimbursements/{batch_id}", str(exc), "warning")
    target_label = "恢复为可报销" if target_state == "ready" else "标记为已排除"
    message = f"已移出 {result['removed']} 张发票，{target_label}"
    if result["status"] == "voided":
        message += "；报销单已无发票，状态改为已作废"
    return flash_redirect(f"/reimbursements/{batch_id}", message)


@app.post("/reimbursements/{batch_id}/add")
async def post_add_reimbursement_items(request: Request, batch_id: int):
    form = await request.form()
    ids = _int_values(form.getlist("ids"))
    if not ids:
        return flash_redirect(f"/reimbursements/{batch_id}", "请先勾选要补充的发票", "warning")
    try:
        result = add_invoices_to_reimbursement(batch_id, ids, _truthy(form.get("mark_reported")))
    except ValueError as exc:
        return flash_redirect(f"/reimbursements/{batch_id}", str(exc), "warning")
    message = f"已补充 {result['added']} 张发票，合计 ¥{result['total_cents'] / 100:.2f}"
    if result["marked_reported"]:
        message += "；新发票已标记为已报销"
    return flash_redirect(f"/reimbursements/{batch_id}", message)


@app.post("/reimbursements/{batch_id}/status")
async def post_reimbursement_status(request: Request, batch_id: int):
    form = await request.form()
    status = form.get("status", "")
    try:
        result = set_reimbursement_status(batch_id, status)
    except ValueError as exc:
        return flash_redirect(f"/reimbursements/{batch_id}", str(exc), "warning")
    message = f"报销单状态已改为{BATCH_STATUS_LABELS.get(status, status)}"
    if result["marked"]:
        message += f"，{result['marked']} 张发票标记为已报销"
    return flash_redirect(f"/reimbursements/{batch_id}", message)


@app.post("/reimbursements/{batch_id}/note")
async def post_reimbursement_note(request: Request, batch_id: int):
    form = await request.form()
    try:
        update_reimbursement_note(batch_id, form.get("note", ""))
    except ValueError as exc:
        return flash_redirect(f"/reimbursements/{batch_id}", str(exc), "warning")
    return flash_redirect(f"/reimbursements/{batch_id}", "备注已保存")


@app.post("/reimbursements/{batch_id}/void")
async def post_void_reimbursement(request: Request, batch_id: int):
    form = await request.form()
    target_state = form.get("target_state", "ready")
    if target_state not in ("ready", "excluded"):
        target_state = "ready"
    try:
        result = void_reimbursement(batch_id, target_state)
    except ValueError as exc:
        return flash_redirect(f"/reimbursements/{batch_id}", str(exc), "warning")
    target_label = "恢复为可报销" if target_state == "ready" else "标记为已排除"
    return flash_redirect(
        f"/reimbursements/{batch_id}",
        f"整单退回完成：{result['removed']} 张发票{target_label}，报销单已作废")


@app.post("/reimbursements/{batch_id}/rebuild")
async def post_rebuild_reimbursement(request: Request, batch_id: int):
    cfg = request.app.state.config
    form = await request.form()
    batch = get_reimbursement(batch_id)
    if not batch:
        raise HTTPException(404, "报销单不存在")
    if not batch["items"]:
        return flash_redirect(f"/reimbursements/{batch_id}", "报销单里没有发票", "warning")
    export_mode = form.get("export_mode") or batch.get("export_mode") or "invoice_only"
    package_mode = form.get("package_mode") or batch.get("package_mode") or "merged_pdf"
    label = f"单据{batch_id}_修正_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    try:
        links = build_export_files(cfg, batch["items"], label, export_mode, package_mode)
    except Exception as exc:
        return flash_redirect(f"/reimbursements/{batch_id}", f"重新生成失败: {exc}", "error")
    attach_reimbursement_files(batch_id, [link["path"] for link in links],
                               export_mode, package_mode)
    return flash_redirect(
        f"/reimbursements/{batch_id}",
        f"已按 {len(batch['items'])} 张发票重新生成 {len(links)} 个文件")


# ── 智能凑票 ────────────────────────────────────────


@app.post("/export/match")
async def post_match_amount(request: Request):
    form = await request.form()

    try:
        target_amount = float(form.get("target_amount", 0))
    except (ValueError, TypeError):
        return flash_redirect("/export", "请输入有效金额", "warning")

    if target_amount <= 0:
        return flash_redirect("/export", "目标金额必须大于0", "warning")

    try:
        max_count = int(form.get("max_count", 0))
    except (ValueError, TypeError):
        max_count = 0
    # 0 代表不限制张数
    if max_count <= 0:
        max_count = 9999

    # 必须包含的发票号码
    required_numbers_str = form.get("required_numbers", "").strip()
    required_invoices: list[dict] = []
    if required_numbers_str:
        nums_list = [n.strip() for n in required_numbers_str.split(",") if n.strip()]
        if not nums_list:
            return flash_redirect("/export", "必选发票号码格式无效", "warning")
        nums_set = set(nums_list)
        # 从可报销池中按号码匹配
        pool = reimbursable_invoices()
        required_invoices = [i for i in pool if i.get("invoice_number") and i["invoice_number"] in nums_set]
        if len(required_invoices) != len(nums_set):
            return flash_redirect("/export", "必选发票号码包含不存在或不可报销的项", "warning")

    # Build filters for candidate invoices
    filters = {}
    for key in ("date_from", "date_to", "project", "person"):
        val = form.get(key, "").strip()
        if val:
            filters[key] = val
    filters["only_included"] = True  # Only reimbursable invoices

    # Get candidate invoices
    from invoice_clipper.matcher import (
        find_multiple_candidates, find_multiple_candidates_with_required,
    )
    candidates_raw = reimbursable_invoices(filters)

    total_available = sum(i.get("amount_with_tax") or 0 for i in candidates_raw)

    # 验证必选发票都在候选池中
    required_numbers_set = {inv["invoice_number"] for inv in required_invoices}
    if required_numbers_set:
        raw_numbers = {i.get("invoice_number") for i in candidates_raw}
        missing = required_numbers_set - raw_numbers
        if missing:
            missing_label = ",".join(missing)
            return flash_redirect(
                "/export",
                f"必选发票号码 {missing_label} 不在当前筛选范围内，请放宽筛选条件",
                "warning",
            )

    # Run matching algorithm
    if required_invoices:
        candidates = find_multiple_candidates_with_required(
            candidates_raw, target_amount, required_invoices,
            count=3, max_count=max_count,
        )
    else:
        candidates = find_multiple_candidates(
            candidates_raw, target_amount, count=3, max_count=max_count,
        )

    response = get_export(request)
    response.context.update({
        "target_amount": target_amount,
        "max_count": max_count,
        "candidates": candidates,
        "total_candidates": len(candidates_raw),
        "total_available": total_available,
        "match_filters": filters,
        "required_numbers": required_numbers_str,
        "required_invoices": required_invoices,
        "required_numbers_set": required_numbers_set,
    })
    return templates.TemplateResponse(request, "export.html", response.context)


# ── Web 启动器（供 invoice-manager-web 命令使用）─

def main():
    """启动 Web UI（供 entry point 和开发使用）"""
    cfg, cfg_path = load_config()
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(cfg.get("server", {}).get("port", 8000))

    url = f"http://{host}:{port}"
    print("发票夹子 v3.6.0 正在启动 ...")
    print(f"   配置文件: {cfg_path}")
    print(f"   本地地址: {url}")

    # 后台打开浏览器
    import threading
    threading.Timer(2.5, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    import uvicorn
    cfg, cfg_path = load_config()
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(cfg.get("server", {}).get("port", 8000))
    print(f"配置文件: {cfg_path}")
    uvicorn.run("invoice_clipper.web:app", host=host, port=port, reload=True)
