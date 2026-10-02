"""
发票夹子 MCP Server (v1.1) — 标签支持

通过 MCP (Model Context Protocol) 将发票管理器功能暴露为 AI Agent 可调用的工具。

启动方式（stdio 模式，供 MCP Client 调用）:
    python -m invoice_clipper.mcp_server

测试方式:
    python -c "from invoice_clipper.mcp_server import mcp; print(mcp.list_tools())"
"""
import logging
import json
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from invoice_clipper import (
    load_config, init_db,
    query_invoices, update_invoice, update_invoice_status,
    get_invoice_by_id, get_all_invoices, delete_invoice,
    get_attachments,
    get_projects, add_project, delete_project,
    get_persons, add_person, delete_person,
    get_tags, add_tag, delete_tag, get_invoice_tags, set_invoice_tags,
    InvoiceProcessor, export_excel, export_merged_pdf, build_export_label,
)
from invoice_clipper.workflow import (
    change_invoice_state, delete_invoice_with_files, reimbursable_invoices,
    record_reimbursement, amount_cents,
)

# ── 日志 ──────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# ── MCP 应用 ──────────────────────────────────────
mcp = FastMCP(
    "发票夹子发票管理器",
    host="127.0.0.1",
    port=8100,
    streamable_http_path="/",     # Mount 后完整路径为 /mcp
    # 本地服务，禁用 DNS rebinding 保护
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# ── 惰性初始化 ────────────────────────────────────
_config = None
_processor = None


def _ensure_ready():
    """确保配置和数据库已初始化（首次调用时惰性加载）"""
    global _config, _processor
    if _config is None:
        cfg, cfg_path = load_config()
        init_db(cfg)
        _config = cfg
        logging.getLogger(__name__).info(f"配置已加载: {cfg_path}")
    if _processor is None:
        _processor = InvoiceProcessor(_config)


# ── 工具：扫描 ────────────────────────────────────

@mcp.tool(
    description="扫描所有监控目录中的新发票文件，自动识别并入库。返回处理结果列表，包含成功和失败数量。"
)
def scan_invoices() -> str:
    """Scan configured watch directories for new invoice files"""
    _ensure_ready()
    proc = _processor
    total = 0
    results = []

    for watch_dir in _config.get("watch_dirs", []):
        watch_path = Path(watch_dir).expanduser()
        if watch_path.exists():
            items = proc.process_directory(watch_path, source="dir")
            total += len(items)
            for item in items:
                results.append({
                    "id": item.get("id"),
                    "seller": item.get("seller_name"),
                    "amount": item.get("amount_with_tax"),
                    "date": item.get("invoice_date"),
                })
        else:
            results.append({"warning": f"监控目录不存在: {watch_path}"})

    return json.dumps({
        "total_processed": total,
        "details": results,
    }, ensure_ascii=False)


# ── 工具：列表与查询 ──────────────────────────────

@mcp.tool(
    description="列出所有发票，可按关键词搜索（卖家/买家/发票号/商品名），按状态筛选（正常/排除）。"
)
def list_invoices(
    search: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """
    列出发票列表
    
    Args:
        search: 搜索关键词（匹配卖家/买家/发票号/商品名）
        status: 筛选状态 - "正常" / "排除" / "全部"（默认"全部"）
    """
    _ensure_ready()
    invoices = get_all_invoices()
    invoices.sort(key=lambda i: i.get("invoice_date") or "", reverse=True)

    if status == "正常":
        invoices = [i for i in invoices if i.get("status") == "ready"]
    elif status == "排除":
        invoices = [i for i in invoices if i.get("status") == "excluded"]
    elif status == "待确认":
        invoices = [i for i in invoices if i.get("status") == "pending"]
    elif status == "已报销":
        invoices = [i for i in invoices if i.get("status") == "reported"]

    if search:
        search_lower = search.strip().lower()
        invoices = [
            i for i in invoices
            if (search_lower in (i.get("seller_name") or "").lower()
                or search_lower in (i.get("buyer_name") or "").lower()
                or search_lower in (i.get("invoice_number") or "").lower()
                or search_lower in (i.get("commodity_name") or "").lower())
        ]

    return json.dumps({
        "count": len(invoices),
        "invoices": [{
            "id": inv.get("id"),
            "invoice_number": inv.get("invoice_number"),
            "invoice_date": inv.get("invoice_date"),
            "seller_name": inv.get("seller_name"),
            "buyer_name": inv.get("buyer_name"),
            "amount_with_tax": inv.get("amount_with_tax"),
            "category": inv.get("category"),
            "belong_project": inv.get("belong_project") or "",
            "belong_person": inv.get("belong_person") or "",
            "excluded": bool(inv.get("excluded")),
            "status": inv.get("status"),
        } for inv in invoices],
    }, ensure_ascii=False)


@mcp.tool(
    description="多条件查询发票，支持按日期范围、卖家、买家、归属项目、归属人筛选。"
)
def query_invoices_tool(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    seller: Optional[str] = None,
    buyer: Optional[str] = None,
    project: Optional[str] = None,
    person: Optional[str] = None,
    only_included: Optional[bool] = None,
) -> str:
    """
    查询发票
    
    Args:
        date_from: 开始日期（YYYY-MM-DD）
        date_to: 结束日期（YYYY-MM-DD）
        seller: 销售方名称（模糊匹配）
        buyer: 购买方名称（模糊匹配）
        project: 归属项目（精确匹配）
        person: 归属人（精确匹配）
        only_included: 仅查可报销的
    """
    _ensure_ready()
    filters = {}
    if date_from:
        filters["date_from"] = date_from
    if date_to:
        filters["date_to"] = date_to
    if seller:
        filters["seller"] = seller
    if buyer:
        filters["buyer"] = buyer
    if project:
        filters["project"] = project
    if person:
        filters["person"] = person
    if only_included:
        filters["only_included"] = True

    invoices = query_invoices(filters)
    total_amount = sum(i.get("amount_with_tax") or 0 for i in invoices)

    return json.dumps({
        "count": len(invoices),
        "total_amount": round(total_amount, 2),
        "invoices": [{
            "id": inv.get("id"),
            "invoice_number": inv.get("invoice_number"),
            "invoice_date": inv.get("invoice_date"),
            "seller_name": inv.get("seller_name"),
            "buyer_name": inv.get("buyer_name"),
            "amount_with_tax": inv.get("amount_with_tax"),
            "tax_amount": inv.get("tax_amount"),
            "category": inv.get("category"),
            "belong_project": inv.get("belong_project") or "",
            "belong_person": inv.get("belong_person") or "",
            "excluded": bool(inv.get("excluded")),
            "status": inv.get("status"),
        } for inv in invoices],
    }, ensure_ascii=False)


# ── 工具：单张发票详情 ────────────────────────────

@mcp.tool(
    description="获取单张发票的完整信息，含附件列表。"
)
def get_invoice(invoice_id: int) -> str:
    """
    获取发票详情
    
    Args:
        invoice_id: 发票 ID
    """
    _ensure_ready()
    inv = get_invoice_by_id(invoice_id)
    if not inv:
        return json.dumps({"error": f"发票 #{invoice_id} 不存在"}, ensure_ascii=False)

    inv = dict(inv)
    attachments = get_attachments(invoice_id)
    inv["attachments"] = [
        {
            "id": att["id"],
            "original_name": att.get("original_name"),
            "file_type": att.get("file_type"),
            "file_size": att.get("file_size"),
        }
        for att in attachments
    ]

    return json.dumps(inv, ensure_ascii=False, default=str)


# ── 工具：更新发票 ────────────────────────────────

@mcp.tool(
    description="更新发票的字段（归属项目/归属人/备注/类别/金额等），仅传入需要修改的字段即可。"
)
def update_invoice_tool(
    invoice_id: int,
    belong_project: Optional[str] = None,
    belong_person: Optional[str] = None,
    remark: Optional[str] = None,
    category: Optional[str] = None,
    commodity_name: Optional[str] = None,
    invoice_date: Optional[str] = None,
    amount_with_tax: Optional[float] = None,
    tax_amount: Optional[float] = None,
    seller_name: Optional[str] = None,
    buyer_name: Optional[str] = None,
) -> str:
    """
    更新发票信息
    
    Args:
        invoice_id: 发票 ID
        belong_project: 归属项目
        belong_person: 归属人
        remark: 备注
        category: 类别（餐饮/交通/办公/服务/其他）
        commodity_name: 商品名称
        invoice_date: 开票日期（YYYY-MM-DD）
        amount_with_tax: 价税合计金额
        tax_amount: 税额
        seller_name: 销售方名称
        buyer_name: 购买方名称
    """
    _ensure_ready()
    inv = get_invoice_by_id(invoice_id)
    if not inv:
        return json.dumps({"error": f"发票 #{invoice_id} 不存在"}, ensure_ascii=False)

    updates = {}
    for key, val in [
        ("belong_project", belong_project),
        ("belong_person", belong_person),
        ("remark", remark),
        ("category", category),
        ("commodity_name", commodity_name),
        ("invoice_date", invoice_date),
        ("amount_with_tax", amount_with_tax),
        ("tax_amount", tax_amount),
        ("seller_name", seller_name),
        ("buyer_name", buyer_name),
    ]:
        if val is not None:
            updates[key] = val

    if not updates:
        return json.dumps({"warning": "没有传入需要更新的字段"}, ensure_ascii=False)

    update_invoice(invoice_id, updates)
    return json.dumps({"success": True, "updated_fields": list(updates.keys()), "invoice_id": invoice_id}, ensure_ascii=False)


# ── 工具：报销状态 ────────────────────────────────

@mcp.tool(
    description="将发票标记为「不报销」（排除）。"
)
def exclude_invoice(invoice_id: int) -> str:
    """
    标记发票为不报销
    
    Args:
        invoice_id: 发票 ID
    """
    _ensure_ready()
    inv = get_invoice_by_id(invoice_id)
    if not inv:
        return json.dumps({"error": f"发票 #{invoice_id} 不存在"}, ensure_ascii=False)
    change_invoice_state(invoice_id, "excluded")
    return json.dumps({"success": True, "invoice_id": invoice_id, "status": "excluded"}, ensure_ascii=False)


@mcp.tool(
    description="将发票恢复为「可报销」。"
)
def include_invoice(invoice_id: int) -> str:
    """
    恢复发票为可报销
    
    Args:
        invoice_id: 发票 ID
    """
    _ensure_ready()
    inv = get_invoice_by_id(invoice_id)
    if not inv:
        return json.dumps({"error": f"发票 #{invoice_id} 不存在"}, ensure_ascii=False)
    change_invoice_state(invoice_id, "ready")
    return json.dumps({"success": True, "invoice_id": invoice_id, "status": "reimbursable"}, ensure_ascii=False)


# ── 工具：删除 ────────────────────────────────────

@mcp.tool(
    description="删除发票及其附件（不可恢复）。"
)
def delete_invoice_tool(invoice_id: int) -> str:
    """
    删除发票

    Args:
        invoice_id: 发票 ID
    """
    _ensure_ready()
    inv = get_invoice_by_id(invoice_id)
    if not inv:
        return json.dumps({"error": f"发票 #{invoice_id} 不存在"}, ensure_ascii=False)

    try:
        delete_invoice_with_files(invoice_id)
    except ValueError as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
    return json.dumps({"success": True, "invoice_id": invoice_id, "action": "deleted"}, ensure_ascii=False)


# ── 工具：处理文件 ────────────────────────────────

@mcp.tool(
    description="处理单个发票文件（PDF/OFD/图片），自动识别并入库。文件路径必须是服务器可访问的绝对路径。"
)
def process_file(file_path: str, source: Optional[str] = "manual") -> str:
    """
    处理单个发票文件

    Args:
        file_path: 文件绝对路径（.pdf/.ofd/.png/.jpg/.jpeg/.bmp/.tiff）
        source: 来源（manual/web/dir），默认 manual
    """
    _ensure_ready()
    path = Path(file_path)
    if not path.exists():
        return json.dumps({"error": f"文件不存在: {file_path}"}, ensure_ascii=False)

    proc = _processor
    result = proc.process_file(path, source=source)

    if result:
        return json.dumps({
            "success": True,
            "id": result.get("id"),
            "invoice_number": result.get("invoice_number"),
            "seller_name": result.get("seller_name"),
            "amount_with_tax": result.get("amount_with_tax"),
            "invoice_date": result.get("invoice_date"),
        }, ensure_ascii=False)
    else:
        return json.dumps({
            "success": False,
            "error": "处理失败（可能是重复发票或识别错误）",
        }, ensure_ascii=False)


# ── 工具：统计 ────────────────────────────────────

@mcp.tool(
    description="获取发票统计数据：总数、可报销数、排除数、总金额、可报销金额。"
)
def get_invoice_stats() -> str:
    """获取发票统计数据"""
    _ensure_ready()
    invoices = get_all_invoices()
    total = len(invoices)
    ok_count = sum(1 for i in invoices if i.get("status") == "ready")
    excluded_count = sum(1 for i in invoices if i.get("status") == "excluded")
    total_amount = sum(i.get("amount_with_tax") or 0 for i in invoices)
    reimbursable_amount = sum(i.get("amount_with_tax") or 0 for i in invoices if i.get("status") == "ready")

    return json.dumps({
        "total_invoices": total,
        "reimbursable_count": ok_count,
        "excluded_count": excluded_count,
        "total_amount": round(total_amount, 2),
        "reimbursable_amount": round(reimbursable_amount, 2),
    }, ensure_ascii=False)


# ── 工具：归属项目管理 ────────────────────────────

@mcp.tool(
    description="列出所有归属项目。"
)
def list_projects() -> str:
    """列出归属项目"""
    _ensure_ready()
    projects = get_projects()
    return json.dumps(projects, ensure_ascii=False)


@mcp.tool(
    description="创建新的归属项目。"
)
def create_project(name: str) -> str:
    """
    创建归属项目

    Args:
        name: 项目名称
    """
    _ensure_ready()
    try:
        pid = add_project(name.strip())
        return json.dumps({"success": True, "id": pid, "name": name.strip()}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    description="删除归属项目（仅当无发票引用时才能删除）。"
)
def delete_project_tool(project_id: int) -> str:
    """
    删除归属项目

    Args:
        project_id: 项目 ID
    """
    _ensure_ready()
    ok = delete_project(project_id)
    if ok:
        return json.dumps({"success": True, "project_id": project_id}, ensure_ascii=False)
    return json.dumps({"error": "该项目已被发票引用，无法删除"}, ensure_ascii=False)


# ── 工具：归属人管理 ────────────────────────────

@mcp.tool(
    description="列出所有归属人。"
)
def list_persons() -> str:
    """列出归属人"""
    _ensure_ready()
    persons = get_persons()
    return json.dumps(persons, ensure_ascii=False)


@mcp.tool(
    description="创建新的归属人（报销人）。"
)
def create_person(name: str) -> str:
    """
    创建归属人

    Args:
        name: 归属人姓名
    """
    _ensure_ready()
    try:
        pid = add_person(name.strip())
        return json.dumps({"success": True, "id": pid, "name": name.strip()}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    description="删除归属人（仅当无发票引用时才能删除）。"
)
def delete_person_tool(person_id: int) -> str:
    """
    删除归属人

    Args:
        person_id: 归属人 ID
    """
    _ensure_ready()
    ok = delete_person(person_id)
    if ok:
        return json.dumps({"success": True, "person_id": person_id}, ensure_ascii=False)
    return json.dumps({"error": "该归属人已被发票引用，无法删除"}, ensure_ascii=False)


# ── 标签管理工具 ────────────────────────────────

@mcp.tool(
    description="列出所有标签。"
)
def list_tags() -> str:
    """列出所有标签"""
    _ensure_ready()
    tags = get_tags()
    return json.dumps(tags, ensure_ascii=False)


@mcp.tool(
    description="创建新标签。"
)
def create_tag(name: str, color: str = "#3b82f6") -> str:
    """
    创建标签

    Args:
        name: 标签名称
        color: 颜色十六进制值（如 #3b82f6）
    """
    _ensure_ready()
    try:
        tid = add_tag(name.strip(), color)
        return json.dumps({"success": True, "id": tid, "name": name.strip(), "color": color}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    description="删除标签（从所有发票中移除此标签）。"
)
def delete_tag_tool(tag_id: int) -> str:
    """
    删除标签

    Args:
        tag_id: 标签 ID
    """
    _ensure_ready()
    ok = delete_tag(tag_id)
    if ok:
        return json.dumps({"success": True, "tag_id": tag_id}, ensure_ascii=False)
    return json.dumps({"error": "删除标签失败"}, ensure_ascii=False)


@mcp.tool(
    description="获取发票的标签列表。"
)
def get_invoice_tags_tool(invoice_id: int) -> str:
    """
    获取发票的标签

    Args:
        invoice_id: 发票 ID
    """
    _ensure_ready()
    tags = get_invoice_tags(invoice_id)
    return json.dumps(tags, ensure_ascii=False)


@mcp.tool(
    description="设置发票的标签（全量替换，传入空的 tag_ids 可清空所有标签）。"
)
def set_invoice_tags_tool(invoice_id: int, tag_ids: list[int]) -> str:
    """
    设置发票标签

    Args:
        invoice_id: 发票 ID
        tag_ids: 标签 ID 列表，传入 [] 清空标签
    """
    _ensure_ready()
    inv = get_invoice_by_id(invoice_id)
    if not inv:
        return json.dumps({"error": f"发票 #{invoice_id} 不存在"}, ensure_ascii=False)
    set_invoice_tags(invoice_id, tag_ids)
    return json.dumps({"success": True, "invoice_id": invoice_id, "tag_ids": tag_ids}, ensure_ascii=False)


# ── 工具：导出 ────────────────────────────────────

@mcp.tool(
    description="导出发票为 Excel 文件（.xlsx），返回导出文件路径。支持按日期/卖家/项目/归属人筛选。"
)
def export_invoices_excel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    seller: Optional[str] = None,
    buyer: Optional[str] = None,
    project: Optional[str] = None,
    person: Optional[str] = None,
    only_included: Optional[bool] = True,
) -> str:
    """
    导出为 Excel

    Args:
        date_from: 开始日期（YYYY-MM-DD）
        date_to: 结束日期（YYYY-MM-DD）
        seller: 销售方名称
        buyer: 购买方名称
        project: 归属项目
        person: 归属人
        only_included: 仅导出可报销发票（默认 true）
    """
    _ensure_ready()
    filters = {}
    for k, v in [("date_from", date_from), ("date_to", date_to),
                 ("seller", seller), ("buyer", buyer),
                 ("project", project), ("person", person)]:
        if v:
            filters[k] = v
    if only_included:
        filters["only_included"] = True

    invoices = reimbursable_invoices(filters) if only_included else query_invoices({**filters, "only_included": False})

    if not invoices:
        return json.dumps({"warning": "没有符合条件的发票", "count": 0}, ensure_ascii=False)

    export_dir = Path.home() / "Documents" / "发票夹子" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    label = build_export_label(filters) if filters else "全部"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    excel_path = export_dir / f"报销明细_{label}_{timestamp}.xlsx"

    export_excel(invoices, excel_path)
    total_amount = sum(amount_cents(i.get("amount_with_tax")) for i in invoices) / 100
    if only_included:
        record_reimbursement(invoices, [excel_path])

    return json.dumps({
        "success": True,
        "file_path": str(excel_path),
        "invoice_count": len(invoices),
        "total_amount": round(total_amount, 2),
        "export_format": "excel",
    }, ensure_ascii=False)


@mcp.tool(
    description="导出发票为合并 PDF 文件，返回导出文件路径。支持按日期/卖家/项目/归属人筛选。"
)
def export_invoices_pdf(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    seller: Optional[str] = None,
    buyer: Optional[str] = None,
    project: Optional[str] = None,
    person: Optional[str] = None,
    only_included: Optional[bool] = True,
) -> str:
    """
    导出为合并 PDF

    Args:
        date_from: 开始日期（YYYY-MM-DD）
        date_to: 结束日期（YYYY-MM-DD）
        seller: 销售方名称
        buyer: 购买方名称
        project: 归属项目
        person: 归属人
        only_included: 仅导出可报销发票（默认 true）
    """
    _ensure_ready()
    filters = {}
    for k, v in [("date_from", date_from), ("date_to", date_to),
                 ("seller", seller), ("buyer", buyer),
                 ("project", project), ("person", person)]:
        if v:
            filters[k] = v
    if only_included:
        filters["only_included"] = True

    invoices = reimbursable_invoices(filters) if only_included else query_invoices({**filters, "only_included": False})

    if not invoices:
        return json.dumps({"warning": "没有符合条件的发票", "count": 0}, ensure_ascii=False)

    export_dir = Path.home() / "Documents" / "发票夹子" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    label = build_export_label(filters) if filters else "全部"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    pdf_path = export_dir / f"报销发票_{label}_{timestamp}.pdf"

    result = export_merged_pdf(invoices, pdf_path)
    total_amount = sum(amount_cents(i.get("amount_with_tax")) for i in invoices) / 100

    if result:
        if only_included:
            record_reimbursement(invoices, [pdf_path])
        return json.dumps({
            "success": True,
            "file_path": str(pdf_path),
            "invoice_count": len(invoices),
            "total_amount": round(total_amount, 2),
            "export_format": "pdf",
        }, ensure_ascii=False)
    else:
        return json.dumps({"error": "合并 PDF 导出失败"}, ensure_ascii=False)


# ── 主入口 ────────────────────────────────────────

def main():
    """启动 MCP Server，传输模式由 config.yaml 的 mcp.transport 决定（可通过 --transport 覆盖）"""
    import sys

    # 解析 --transport 参数覆盖配置
    cli_transport = None
    args = [a for a in sys.argv[1:] if not a.startswith("--transport=")]
    for i, a in enumerate(sys.argv[1:], 1):
        if a == "--transport" and i < len(sys.argv):
            cli_transport = sys.argv[i + 1]
        elif a.startswith("--transport="):
            cli_transport = a.split("=", 1)[1]

    cfg, cfg_path = load_config()
    mcp_cfg = cfg.get("mcp", {})
    transport_setting = cli_transport or mcp_cfg.get("transport", "http")

    # 将便捷名称映射为 MCP SDK 标准名称
    transport_map = {"http": "streamable-http", "sse": "sse", "stdio": "stdio", "streamable-http": "streamable-http"}
    transport = transport_map.get(transport_setting, "streamable-http")

    # 独立运行时的默认地址（捆绑 Web 启动时走 server.port）
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = 8100

    print(f"📋 MCP Server: {transport} mode")
    if transport == "streamable-http":
        print(f"🌐 Listening on http://{mcp.settings.host}:{mcp.settings.port}/mcp")
    elif transport == "sse":
        print(f"🌐 Listening on http://{mcp.settings.host}:{mcp.settings.port}/sse")

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
