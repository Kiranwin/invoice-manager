#!/usr/bin/env python3
"""发票夹子 Web UI — FastAPI + Jinja2 (v3.4.0)"""
import re
import json
import shutil
import sys
import os
import webbrowser
import tempfile
import zipfile
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, Form, File, UploadFile, Query, HTTPException
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from invoice_clipper import (
    load_config, init_db, query_invoices, update_invoice_status, update_invoice,
    get_invoice_by_id, get_all_invoices, delete_invoice,
    get_attachments, insert_attachment, delete_attachment,
    get_projects, add_project, delete_project,
    get_persons, add_person, delete_person,
    get_tags, add_tag, delete_tag, get_invoice_tags, set_invoice_tags,
    get_all_invoice_tags, get_invoices_by_ids, search_invoices_by_tags,
    InvoiceProcessor, export_excel, export_merged_pdf, build_export_label,
    export_zip_sources,
    build_attachment_path, next_attachment_seq,
)
from contextlib import asynccontextmanager

from invoice_clipper.mcp_server import mcp as mcp_server
from invoice_clipper.workflow import (
    change_invoice_state, delete_invoice_with_files, reimbursable_invoices,
    record_reimbursement, list_reimbursements, amount_cents,
    record_import_failure, list_import_failures, retry_import_failure,
)

# 包内资源路径（安装后模板/静态文件在包目录内）
PKG_DIR = Path(__file__).parent


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期：加载配置 + 启动 MCP Streamable HTTP session manager"""
    cfg, cfg_path = load_config()
    app.state.config = cfg
    app.state.config_path = cfg_path
    init_db(cfg)

    # 提前调用确保 _session_manager 已创建
    _mcp_starlette = mcp_server.streamable_http_app()
    async with mcp_server._session_manager.run():
        yield  # 应用运行中


app = FastAPI(title="发票夹子", version="3.4.0", lifespan=_lifespan)
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
    return [i for i in get_all_invoices() if i.get("status") == "pending"]


@app.get("/inbox")
def get_inbox(request: Request):
    """Unified inbox: upload new files and review newly recognized invoices."""
    ctx = get_flash(request)
    ctx.update({
        "page": "inbox",
        "results": None,
        "summary": None,
        "pending_invoices": _pending_invoices(),
        "failures": list_import_failures(),
        "projects": get_projects(),
        "persons": get_persons(),
    })
    return templates.TemplateResponse(request, "scan.html", ctx)


@app.post("/inbox")
async def post_scan(request: Request, files: list[UploadFile] = File(...)):
    cfg = request.app.state.config
    form = await request.form()
    belong_project = form.get("belong_project", "").strip()
    belong_person = form.get("belong_person", "").strip()

    proc = InvoiceProcessor(cfg)
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
                record_import_failure(f.filename or tmp.name, tmp, proc._error or "识别失败")
            results.append({
                "id": r.get("id", "?") if r else "?",
                "filename": f.filename or "-",
                "ok": r is not None,
                "error": proc._error if not r else None,
                "invoice_number": r.get("invoice_number", "-") if r else "-",
                "invoice_date": r.get("invoice_date", "-") if r else "-",
                "seller_name": r.get("seller_name", "-") if r else "-",
                "amount_with_tax": r.get("amount_with_tax", 0) if r else 0,
                "belong_project": belong_project or "",
                "belong_person": belong_person or "",
            })
        finally:
            if r is not None:
                tmp.unlink(missing_ok=True)

    ok_count = sum(1 for r in results if r["ok"])
    ctx = get_flash(request)
    ctx.update({
        "page": "inbox",
        "results": results,
        "summary": {"ok_count": ok_count, "total_count": len(results)},
        "pending_invoices": _pending_invoices(),
        "failures": list_import_failures(),
        "projects": get_projects(),
        "persons": get_persons(),
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


@app.get("/list")
def get_list(request: Request, search: str = "", status: list[str] = Query(default=["待确认", "正常"])):
    filters = build_filters(request)
    filters["only_included"] = False
    invoices = query_invoices(filters) if any(k != "only_included" for k in filters) else get_all_invoices()
    tag_id = request.query_params.get("tag_id", "")
    if tag_id.isdigit():
        tagged_ids = {i["id"] for i in search_invoices_by_tags([int(tag_id)])}
        invoices = [i for i in invoices if i["id"] in tagged_ids]
    invoices.sort(key=lambda i: i.get("invoice_date") or "", reverse=True)

    state_names = {"待确认": "pending", "正常": "ready", "排除": "excluded", "已报销": "reported"}
    selected_states = {state_names[name] for name in status if name in state_names}
    invoices = [i for i in invoices if i.get("status") in selected_states]

    search_lower = search.strip().lower()
    if search_lower:
        invoices = [
            i for i in invoices
            if (search_lower in (i.get("seller_name") or "").lower()
                or search_lower in (i.get("buyer_name") or "").lower()
                or search_lower in (i.get("invoice_number") or "").lower()
                or search_lower in (i.get("commodity_name") or "").lower())
        ]

    all_invs = get_all_invoices()
    total = len(all_invs)
    ok = sum(1 for i in all_invs if i.get("status") == "ready")
    excluded = sum(1 for i in all_invs if i.get("status") == "excluded")
    pending = sum(1 for i in all_invs if i.get("status") == "pending")
    reported = sum(1 for i in all_invs if i.get("status") == "reported")
    reimbursable = sum(i.get("amount_with_tax") or 0 for i in all_invs if i.get("status") == "ready")
    total_amount = sum(i.get("amount_with_tax") or 0 for i in all_invs)

    ctx = get_flash(request)
    ctx.update({
        "page": "list",
        "invoices": invoices,
        "search": search, "status_filter": status,
        "filters": {key: request.query_params.get(key, "") for key in
                    ("date_from", "date_to", "seller", "buyer", "project", "person")},
        "tag_id": tag_id,
        "all_tags": get_tags(),
        "invoice_tags": get_all_invoice_tags(),
        "projects": get_projects(),
        "persons": get_persons(),
        "stats": {
            "total": total, "ok_count": ok, "excluded_count": excluded,
            "pending_count": pending, "reported_count": reported,
            "reimbursable_amount": reimbursable, "total_amount": total_amount,
        },
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
    cfg = request.app.state.config
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
        "raw_json": json.dumps(dict(inv), ensure_ascii=False, indent=2, default=str),
    })
    return templates.TemplateResponse(request, "edit.html", ctx)


@app.post("/list/{inv_id}")
async def post_edit(request: Request, inv_id: int):
    cfg = request.app.state.config
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
    cfg = request.app.state.config
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
    cfg = request.app.state.config
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
    cfg = request.app.state.config
    atts = get_attachments(inv_id)
    target = next((a for a in atts if a["id"] == att_id), None)
    if target:
        p = Path(target["stored_path"])
        if p.exists():
            p.unlink()
        delete_attachment(att_id)
    return flash_redirect(f"/list/{inv_id}", "附件已删除")


# ── Attachment ZIP export helper ────────────────


def _export_attachments_zip(invoices: list, zip_path: Path, cfg: dict):
    """Build a ZIP of all attachments organized by invoice number"""
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        for inv in invoices:
            inv_id = inv["id"]
            inv_no = (inv.get("invoice_number") or f"INV-{inv_id}").replace("/", "_")
            seller = (inv.get("seller_name") or "unknown")[:20]
            folder = f"{inv_no}_{seller}/"
            atts = get_attachments(inv_id)
            for att in atts:
                fp = Path(att["stored_path"])
                if fp.exists():
                    arcname = folder + (att.get("original_name") or fp.name)
                    zf.write(str(fp), arcname)


def _export_dir(cfg: dict) -> Path:
    configured = cfg.get("storage", {}).get("export_dir")
    return Path(configured).expanduser() if configured else Path.home() / "Documents" / "发票夹子" / "exports"


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

    ctx.update({
        "page": "export",
        "filters": {"date_from": "", "date_to": "", "seller": "", "buyer": "",
                     "project": "", "person": ""},
        "download_links": None, "invoice_count": None, "total_amount": 0.0,
        "selected_invoices": selected_invoices,
        "selected_ids": selected_ids_param,
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

    export_dir = _export_dir(cfg)
    export_dir.mkdir(parents=True, exist_ok=True)
    label = build_export_label(form) if not selected_ids_str else f"选中{len(invoices)}张"
    label = f"{label}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    download_links = []

    try:
        # Excel 始终导出
        excel_path = export_dir / f"报销明细_{label}.xlsx"
        export_excel(invoices, excel_path)
        download_links.append({"label": "下载 Excel", "filename": excel_path.name})

        include_attachments = export_mode == "with_attachments"

        # Merged PDF
        if package_mode in ("merged_pdf", "both"):
            att_map = None
            if include_attachments:
                att_map = {}
                for inv in invoices:
                    att_map[inv["id"]] = get_attachments(inv["id"])
            pdf_path = export_dir / f"报销发票_{label}.pdf"
            result = export_merged_pdf(invoices, pdf_path, attachments_map=att_map)
            if result:
                download_links.append({"label": "下载合并 PDF", "filename": pdf_path.name})

        # Source ZIP
        if package_mode in ("source_zip", "both"):
            zip_path = export_dir / f"源文件_{label}.zip"
            result = export_zip_sources(invoices, zip_path, include_attachments=include_attachments)
            if result:
                download_links.append({"label": "下载源文件 ZIP", "filename": zip_path.name})

        # Legacy: attachment-only ZIP (only when mode is with_attachments and package is merged_pdf or both)
        if include_attachments and package_mode == "merged_pdf":
            zip_path = export_dir / f"发票附件_{label}.zip"
            _export_attachments_zip(invoices, zip_path, cfg)
            if zip_path.exists():
                download_links.append({"label": "下载附件 ZIP", "filename": zip_path.name})

    except Exception as e:
        return flash_redirect("/export", f"导出失败: {e}", "error")

    try:
        batch_id = record_reimbursement(
            invoices, [export_dir / link["filename"] for link in download_links],
            mark_reported=bool(form.get("mark_reported")),
        )
    except ValueError as exc:
        return flash_redirect("/export", str(exc), "warning")
    marked_count = len(invoices) if form.get("mark_reported") else 0

    ctx = get_flash(request)
    ctx.update({
        "page": "export",
        "filters": filters if not selected_ids_str else {},
        "download_links": download_links,
        "invoice_count": len(invoices),
        "total_amount": total_amount,
        "selected_ids": selected_ids_str,
        "selected_invoices": invoices if selected_ids_str else [],
        "marked_count": marked_count,
        "batch_id": batch_id,
        "history": list_reimbursements(),
        "target_amount": "",
        "max_count": 0,
        "candidates": None,
        "match_filters": {},
    })
    return templates.TemplateResponse(request, "export.html", ctx)


@app.get("/export/download/{filename}")
def download_export(request: Request, filename: str):
    export_dir = _export_dir(request.app.state.config)
    filepath = export_dir / filename
    if not filepath.resolve().is_relative_to(export_dir.resolve()):
        raise HTTPException(404)
    if not re.match(r'^(报销(明细|发票)|源文件|发票附件)_.+\.(xlsx|pdf|zip)$', filename):
        raise HTTPException(404)
    if not filepath.exists():
        raise HTTPException(404)
    return FileResponse(filepath, filename=filename)


# ── 智能凑票 ────────────────────────────────────────


@app.post("/export/match")
async def post_match_amount(request: Request):
    cfg = request.app.state.config
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

    # Build filters for candidate invoices
    filters = {}
    for key in ("date_from", "date_to", "project", "person"):
        val = form.get(key, "").strip()
        if val:
            filters[key] = val
    filters["only_included"] = True  # Only reimbursable invoices

    # Get candidate invoices
    from invoice_clipper.matcher import find_multiple_candidates
    candidates_raw = reimbursable_invoices(filters)
    
    total_available = sum(i.get("amount_with_tax") or 0 for i in candidates_raw)

    # Run matching algorithm
    candidates = find_multiple_candidates(candidates_raw, target_amount, count=3, max_count=max_count)

    response = get_export(request)
    response.context.update({
        "target_amount": target_amount,
        "max_count": max_count,
        "candidates": candidates,
        "total_candidates": len(candidates_raw),
        "total_available": total_available,
        "match_filters": filters,
    })
    return templates.TemplateResponse(request, "export.html", response.context)


# ── Web 启动器（供 invoice-manager-web 命令使用）─

def main():
    """启动 Web UI（供 entry point 和开发使用）"""
    cfg, cfg_path = load_config()
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(cfg.get("server", {}).get("port", 8000))

    url = f"http://{host}:{port}"
    print(f"发票夹子 v3.4.0 正在启动 ...")
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
