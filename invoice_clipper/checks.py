"""发票字段自检：给出需要人工确认的点。

识别引擎返回的 confidence 是「关键字段是否齐全」的评分，不是真正的字段级
置信度，所以这里不伪造精度，只做确定性的合理性检查：金额与税额能否对上、
发票号位数、日期是否在合理区间、必填项是否缺失。
"""
import re
from datetime import date, datetime

# 字段名 -> 界面上的中文标签
FIELD_LABELS = {
    "invoice_number": "发票号码",
    "invoice_code": "发票代码",
    "invoice_date": "开票日期",
    "commodity_name": "项目名称",
    "buyer_name": "购买方",
    "buyer_tax_num": "购买方税号",
    "seller_name": "销售方",
    "seller_tax_num": "销售方税号",
    "amount_with_tax": "价税合计",
    "tax_amount": "税额",
    "tax_rate": "税率",
}

# 全电发票 20 位，旧版发票 8 位；其它长度大概率是识别串行
COMMON_INVOICE_NUMBER_LENGTHS = (8, 20)
TAX_NUMBER_LENGTHS = (15, 17, 18, 20)
AMOUNT_TOLERANCE = 0.05  # 税额允许的分位误差

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TAX_NUMBER_PATTERN = re.compile(r"^[0-9A-Z]{15,20}$")


def _to_float(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_cents(value) -> int | None:
    number = _to_float(value)
    return None if number is None else int(round(number * 100))


def invoice_issues(invoice: dict) -> list[dict]:
    """返回按严重度排序的问题列表，每项 {level, field, label, message}。"""
    issues: list[dict] = []

    def add(level: str, field: str, message: str):
        issues.append({
            "level": level,
            "field": field,
            "label": FIELD_LABELS.get(field, field),
            "message": message,
        })

    number = (invoice.get("invoice_number") or "").strip()
    seller = (invoice.get("seller_name") or "").strip()
    buyer = (invoice.get("buyer_name") or "").strip()
    invoice_date = (invoice.get("invoice_date") or "").strip()
    amount = _to_cents(invoice.get("amount_with_tax"))
    tax = _to_cents(invoice.get("tax_amount"))
    rate = _to_float(invoice.get("tax_rate"))

    # ── 必填项 ──────────────────────────────────
    if not number:
        add("error", "invoice_number", "缺少发票号码，无法查重也无法报销")
    else:
        if not number.isalnum():
            add("warn", "invoice_number", "发票号码含非字母数字字符，可能识别串行")
        elif len(number) not in COMMON_INVOICE_NUMBER_LENGTHS:
            add("warn", "invoice_number",
                f"发票号码为 {len(number)} 位，常见是 8 位或 20 位")

    if amount is None:
        add("error", "amount_with_tax", "缺少价税合计")
    elif amount <= 0:
        add("error", "amount_with_tax", "价税合计为 0 或负数")

    if not invoice_date:
        add("error", "invoice_date", "缺少开票日期")
    elif not _DATE_PATTERN.match(invoice_date):
        add("warn", "invoice_date", "开票日期格式不是 YYYY-MM-DD")
    else:
        try:
            parsed = datetime.strptime(invoice_date, "%Y-%m-%d").date()
        except ValueError:
            add("warn", "invoice_date", "开票日期不是有效日期")
        else:
            if parsed.year < 2011:
                add("warn", "invoice_date", f"开票日期是 {parsed.year} 年，请确认是否识别错")
            elif parsed > date.today():
                add("warn", "invoice_date", "开票日期晚于今天")

    if not seller:
        add("warn", "seller_name", "缺少销售方名称")

    # ── 金额关系 ────────────────────────────────
    if amount and tax is not None:
        if tax < 0:
            add("error", "tax_amount", "税额为负数")
        elif tax > amount:
            add("error", "tax_amount", "税额大于价税合计")
        elif rate and rate > 0 and tax > 0:
            rate_value = rate / 100 if rate > 1 else rate
            expected = amount / 100 * rate_value / (1 + rate_value)
            if abs(expected - tax / 100) > AMOUNT_TOLERANCE:
                add("warn", "tax_amount",
                    f"税额与税率对不上：按 {rate_value * 100:.0f}% 计算应约 "
                    f"¥{expected:.2f}，实际 ¥{tax / 100:.2f}")

    if rate is not None:
        rate_value = rate / 100 if rate > 1 else rate
        if rate_value < 0 or rate_value > 1:
            add("warn", "tax_rate", f"税率 {rate_value:.2%} 不在 0-100% 范围内")

    # ── 税号 ────────────────────────────────────
    for field, value, who in (("buyer_tax_num", (invoice.get("buyer_tax_num") or "").strip(), "购买方"),
                              ("seller_tax_num", (invoice.get("seller_tax_num") or "").strip(), "销售方")):
        if value and not _TAX_NUMBER_PATTERN.match(value.upper()):
            add("warn", field, f"{who}税号格式可疑（应为 15-20 位字母数字）")
    if buyer and not (invoice.get("buyer_tax_num") or "").strip():
        add("warn", "buyer_tax_num", "有购买方名称但没有税号，报销时可能被退回")

    level_order = {"error": 0, "warn": 1}
    issues.sort(key=lambda item: level_order.get(item["level"], 9))
    return issues


def issue_summary(issues: list[dict]) -> dict:
    return {
        "total": len(issues),
        "error_count": sum(1 for i in issues if i["level"] == "error"),
        "warn_count": sum(1 for i in issues if i["level"] == "warn"),
        "first": issues[0]["message"] if issues else "",
    }
