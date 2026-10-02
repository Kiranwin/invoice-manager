#!/usr/bin/env python3
"""发票夹子 Web UI — FastAPI + Jinja2 (v3.3.2)"""
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
    get_all_invoice_tags, get_invoices_by_ids,
    InvoiceProcessor, export_excel, export_merged_pdf, build_export_label,
    export_zip_sources,
    build_attachment_path, next_attachment_seq,
)
from contextlib import asynccontextmanager

from invoice_clipper.mcp_server import mcp as mcp_server

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


app = FastAPI(title="发票夹子", version="3.3.2", lifespan=_lifespan)
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
    return RedirectResponse("/list", status_code=303)


@app.get("/scan")
def get_scan(request: Request):
    ctx = get_flash(request)
    ctx.update({
        "page": "scan",
        "results": None,
        "summary": None,
        "projects": get_projects(),
        "persons": get_persons(),
    })
    return templates.TemplateResponse(request, "scan.html", ctx)


@app.post("/scan")
async def post_scan(request: Request, files: list[UploadFile] = File(...)):
    cfg = request.app.state.config
    form = await request.form()
    belong_project = form.get("belong_project", "").strip()
    belong_person = form.get("belong_person", "").strip()

    proc = InvoiceProcessor(cfg)
    results = []

    for f in files:
        safe_name = f"{datetime.now().strftime('%Y%m%d%H%M%S%f')}_{f.filename or 'upload'}"
        tmp = Path(tempfile.gettempdir()) / safe_name
        content = await f.read()
        with open(tmp, "wb") as fp:
            fp.write(content)

        r = proc.process_file(tmp, source="web")
        # 写入归属信息
        if r and (belong_project or belong_person):
            updates = {}
            if belong_project:
                updates["belong_project"] = belong_project
            if belong_person:
                updates["belong_person"] = belong_person
            update_invoice(r["id"], updates)
            r.update(updates)

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
        if tmp.exists():
            tmp.unlink()

    ok_count = sum(1 for r in results if r["ok"])
    ctx = get_flash(request)
    ctx.update({
        "page": "scan",
        "results": results,
        "summary": {"ok_count": ok_count, "total_count": len(results)},
        "projects": get_projects(),
        "persons": get_persons(),
    })
    return templates.TemplateResponse(request, "scan.html", ctx)


@app.get("/list")
def get_list(request: Request, search: str = "", status: list[str] = Query(default=["正常"])):
    invoices = get_all_invoices()
    invoices.sort(key=lambda i: i.get("invoice_date") or "", reverse=True)

    show_normal = "正常" in status
    show_excluded = "排除" in status
    if show_normal and not show_excluded:
        invoices = [i for i in invoices if not i.get("excluded")]
    elif show_excluded and not show_normal:
        invoices = [i for i in invoices if i.get("excluded")]
    elif not show_normal and not show_excluded:
        invoices = []

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
    ok = sum(1 for i in all_invs if not i.get("excluded"))
    excluded = total - ok
    reimbursable = sum(i.get("amount_with_tax") or 0 for i in all_invs if not i.get("excluded"))
    total_amount = sum(i.get("amount_with_tax") or 0 for i in all_invs)

    ctx = get_flash(request)
    ctx.update({
        "page": "list",
        "invoices": invoices,
        "search": search, "status_filter": status,
        "all_tags": get_tags(),
        "invoice_tags": get_all_invoice_tags(),
        "projects": get_projects(),
        "persons": get_persons(),
        "stats": {
            "total": total, "ok_count": ok, "excluded_count": excluded,
            "reimbursable_amount": reimbursable, "total_amount": total_amount,
        },
    })
    return templates.TemplateResponse(request, "list.html", ctx)


# ── 归属管理 ────────────────────────────────────────


@app.get("/assignments")
def get_assignments(request: Request):
    ctx = get_flash(request)
    ctx.update({
        "page": "assignments",
        "projects": get_projects(),
        "persons": get_persons(),
    })
    return templates.TemplateResponse(request, "assignments.html", ctx)


@app.post("/assignments/project")
def add_assignment_project(request: Request, name: str = Form(...)):
    name = name.strip()
    if not name:
        return flash_redirect("/assignments", "项目名称不能为空", "warning")
    try:
        add_project(name)
        return flash_redirect("/assignments", f"归属项目「{name}」已创建")
    except Exception as e:
        return flash_redirect("/assignments", f"创建失败: {e}", "error")


@app.post("/assignments/project/{project_id}/delete")
def delete_assignment_project(project_id: int):
    ok = delete_project(project_id)
    if ok:
        return flash_redirect("/assignments", "归属项目已删除")
    return flash_redirect("/assignments", "该项目已被发票引用，无法删除", "warning")


@app.post("/assignments/person")
def add_assignment_person(request: Request, name: str = Form(...)):
    name = name.strip()
    if not name:
        return flash_redirect("/assignments", "归属人名称不能为空", "warning")
    try:
        add_person(name)
        return flash_redirect("/assignments", f"归属人「{name}」已创建")
    except Exception as e:
        return flash_redirect("/assignments", f"创建失败: {e}", "error")


@app.post("/assignments/person/{person_id}/delete")
def delete_assignment_person(person_id: int):
    ok = delete_person(person_id)
    if ok:
        return flash_redirect("/assignments", "归属人已删除")
    return flash_redirect("/assignments", "该归属人已被发票引用，无法删除", "warning")


# ── 标签管理 ────────────────────────────────────────


@app.get("/tags")
def get_tags_page(request: Request):
    ctx = get_flash(request)
    ctx.update({
        "page": "tags",
        "tags": get_tags(),
    })
    return templates.TemplateResponse(request, "tags.html", ctx)


@app.post("/tags")
def add_tag_route(request: Request, name: str = Form(...), color: str = Form("#3b82f6")):
    name = name.strip()
    if not name:
        return flash_redirect("/tags", "标签名称不能为空", "warning")
    try:
        add_tag(name, color)
        return flash_redirect("/tags", f"标签「{name}」已创建")
    except Exception as e:
        return flash_redirect("/tags", f"创建失败: {e}", "error")


@app.post("/tags/{tag_id}/delete")
def delete_tag_route(tag_id: int):
    ok = delete_tag(tag_id)
    if ok:
        return flash_redirect("/tags", "标签已删除")
    return flash_redirect("/tags", "删除失败", "error")


@app.post("/list/batch-toggle")
def batch_toggle(request: Request, ids: list[int] = Form(...), action: str = Query(...)):
    cfg = request.app.state.config
    excluded = action == "exclude"
    for inv_id in ids:
        update_invoice_status(inv_id, excluded=excluded)
    label = "已排除" if excluded else "已恢复"
    return flash_redirect("/list", f"批量操作完成: {len(ids)} 张发票{label}")


@app.post("/list/batch-delete")
def batch_delete(request: Request, ids: list[int] = Form(...)):
    """批量删除发票"""
    cfg = request.app.state.config
    deleted = 0
    for inv_id in ids:
        inv = get_invoice_by_id(inv_id)
        if not inv:
            continue
        for att in get_attachments(inv_id):
            p = Path(att["stored_path"])
            if p.exists():
                p.unlink()
        stored = inv.get("stored_path")
        if stored:
            p = Path(stored)
            if p.exists():
                p.unlink()
            parent = p.parent
            try:
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
            except OSError:
                pass
        delete_invoice(inv_id)
        deleted += 1
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
    new_status = not inv.get("excluded")
    update_invoice_status(inv_id, excluded=new_status)
    label = "已排除" if new_status else "已恢复"
    return flash_redirect(f"/list/{inv_id}", f"发票 #{inv_id} {label}")


@app.post("/list/{inv_id}/delete")
def delete_invoice_route(request: Request, inv_id: int):
    cfg = request.app.state.config
    inv = get_invoice_by_id(inv_id)
    if not inv:
        raise HTTPException(404)

    for att in get_attachments(inv_id):
        p = Path(att["stored_path"])
        if p.exists():
            p.unlink()

    stored = inv.get("stored_path")
    if stored:
        p = Path(stored)
        if p.exists():
            p.unlink()
        parent = p.parent
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            pass

    delete_invoice(inv_id)
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


@app.get("/query")
def get_query(request: Request):
    cfg = request.app.state.config
    filters = build_filters(request)
    if request.query_params.get("only_included"):
        filters["only_included"] = True

    results = None
    total_amount = 0.0

    # Handle tag filtering
    tag_ids_param = request.query_params.get("tag_ids", "")
    if tag_ids_param and tag_ids_param.strip():
        tag_ids = [int(x) for x in tag_ids_param.split(",") if x.strip().isdigit()]
        # Get invoice IDs matching those tags
        tag_invoices = search_invoices_by_tags(tag_ids)
        tag_inv_ids = [i["id"] for i in tag_invoices]
        # Apply additional filters on top of tag filter
        if filters:
            filters["exclude_ids"] = [i for i in range(-1)]  # dummy - we'll merge differently
            # We need to get base filters then intersect
            base_results = query_invoices(filters)
            base_ids = {r["id"] for r in base_results}
            tag_ids_set = set(tag_inv_ids)
            intersection = base_ids & tag_ids_set
            results = [r for r in base_results if r["id"] in intersection]
        else:
            results = tag_invoices
        total_amount = sum(r.get("amount_with_tax") or 0 for r in results)
    elif filters:
        results = query_invoices(filters)
        total_amount = sum(r.get("amount_with_tax") or 0 for r in results)

    ctx = get_flash(request)
    ctx.update({
        "page": "query",
        "results": results,
        "total_amount": total_amount,
        "all_tags": get_tags(),
        "filters": {
            "date_from": request.query_params.get("date_from", ""),
            "date_to": request.query_params.get("date_to", ""),
            "seller": request.query_params.get("seller", ""),
            "buyer": request.query_params.get("buyer", ""),
            "project": request.query_params.get("project", ""),
            "person": request.query_params.get("person", ""),
            "only_included": request.query_params.get("only_included", ""),
            "tag_ids": request.query_params.get("tag_ids", ""),
        },
    })
    return templates.TemplateResponse(request, "query.html", ctx)


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


@app.get("/export")
def get_export(request: Request):
    ctx = get_flash(request)
    
    # Handle pre-selected invoice IDs from list page
    selected_ids_param = request.query_params.get("ids", "")
    selected_invoices = []
    if selected_ids_param:
        ids_list = [int(x) for x in selected_ids_param.split(",") if x.strip().isdigit()]
        if ids_list:
            selected_invoices = get_invoices_by_ids(ids_list)

    ctx.update({
        "page": "export",
        "filters": {"date_from": "", "date_to": "", "seller": "", "buyer": "",
                     "project": "", "person": ""},
        "download_links": None, "invoice_count": None, "total_amount": 0.0,
        "selected_invoices": selected_invoices,
        "selected_ids": selected_ids_param,
    })
    return templates.TemplateResponse(request, "export.html", ctx)


@app.post("/export")
async def post_export(request: Request):
    cfg = request.app.state.config
    form = await request.form()

    # Determine invoice selection mode
    selected_ids_str = form.get("selected_ids", "").strip()
    if selected_ids_str:
        # Selected-invoice mode: get by IDs
        ids_list = [int(x) for x in selected_ids_str.split(",") if x.strip().isdigit()]
        invoices = get_invoices_by_ids(ids_list)
    else:
        # Filter mode (existing): build filters from form
        filters = {}
        for key in ("date_from", "date_to", "seller", "buyer", "project", "person"):
            val = form.get(key, "").strip()
            if val:
                filters[key] = val
        filters["only_included"] = True
        invoices = query_invoices(filters) if filters else []

    total_amount = sum(i.get("amount_with_tax") or 0 for i in invoices)
    if not invoices:
        return flash_redirect("/export", "没有符合条件的发票", "warning")

    # Export mode
    export_mode = form.get("export_mode", "invoice_only")  # invoice_only | with_attachments
    package_mode = form.get("package_mode", "merged_pdf")  # merged_pdf | source_zip | both

    export_dir = Path.home() / "Documents" / "发票夹子" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    label = build_export_label(form) if not selected_ids_str else f"选中{len(invoices)}张"
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

    # 导出后标记为已报销（复用 excluded 字段：已报销的发票不再参与下次凑票与导出）
    marked_count = 0
    if form.get("mark_reported"):
        for inv in invoices:
            update_invoice_status(inv["id"], excluded=True)
            marked_count += 1

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
    })
    return templates.TemplateResponse(request, "export.html", ctx)


@app.get("/export/download/{filename}")
def download_export(filename: str):
    export_dir = Path.home() / "Documents" / "发票夹子" / "exports"
    filepath = export_dir / filename
    if not filepath.resolve().is_relative_to(export_dir.resolve()):
        raise HTTPException(404)
    if not re.match(r'^(报销(明细|发票)|源文件|发票附件)_.+\.(xlsx|pdf|zip)$', filename):
        raise HTTPException(404)
    if not filepath.exists():
        raise HTTPException(404)
    return FileResponse(filepath, filename=filename)


# ── 智能凑票 ────────────────────────────────────────


@app.get("/match-amount")
def get_match_amount(request: Request):
    ctx = get_flash(request)
    ctx.update({
        "page": "match_amount",
        "target_amount": "",
        "max_count": 0,
        "candidates": None,
        "filters": {},
    })
    return templates.TemplateResponse(request, "match_amount.html", ctx)


@app.post("/match-amount")
async def post_match_amount(request: Request):
    cfg = request.app.state.config
    form = await request.form()

    try:
        target_amount = float(form.get("target_amount", 0))
    except (ValueError, TypeError):
        return flash_redirect("/match-amount", "请输入有效金额", "warning")

    if target_amount <= 0:
        return flash_redirect("/match-amount", "目标金额必须大于0", "warning")

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
    candidates_raw = query_invoices(filters)
    
    total_available = sum(i.get("amount_with_tax") or 0 for i in candidates_raw)

    # Run matching algorithm
    candidates = find_multiple_candidates(candidates_raw, target_amount, count=3, max_count=max_count)

    ctx = get_flash(request)
    ctx.update({
        "page": "match_amount",
        "target_amount": target_amount,
        "max_count": max_count,
        "candidates": candidates,
        "total_candidates": len(candidates_raw),
        "total_available": total_available,
        "filters": filters,
    })
    return templates.TemplateResponse(request, "match_amount.html", ctx)


# ── Web 启动器（供 invoice-manager-web 命令使用）─

def main():
    """启动 Web UI（供 entry point 和开发使用）"""
    cfg, cfg_path = load_config()
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = int(cfg.get("server", {}).get("port", 8000))

    url = f"http://{host}:{port}"
    print(f"发票夹子 v3.3.2 正在启动 ...")
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
