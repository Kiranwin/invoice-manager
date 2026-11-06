#!/usr/bin/env python3
"""零依赖回归自检：把容易改坏的纯逻辑跑一遍。

用法：
    uv run python tools/selfcheck.py

覆盖：
- 发票字段自检（checks.invoice_issues）
- 金额筛选解析（web.parse_amount_filter）
- 金额按分取整（workflow.amount_cents）
- 报销单增删与合计重算（在临时库上跑，不碰真实数据）
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_clipper.checks import invoice_issues
from invoice_clipper.workflow import amount_cents

failures: list[str] = []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        return
    failures.append(f"{name}{('：' + detail) if detail else ''}")


def field_names(issues: list[dict]) -> list[str]:
    return [f"{i['level']}:{i['field']}" for i in issues]


# ── 1. 字段自检 ──────────────────────────────────
def test_checks():
    empty = invoice_issues({"invoice_number": "", "amount_with_tax": 0, "invoice_date": ""})
    check("空发票报必填缺失",
          {"error:invoice_number", "error:amount_with_tax", "error:invoice_date"} <= set(field_names(empty)),
          str(field_names(empty)))

    good = invoice_issues({
        "invoice_number": "24322000000000000001", "invoice_date": "2025-03-01",
        "seller_name": "测试商户", "buyer_name": "中国矿业大学",
        "buyer_tax_num": "12345678901234567X",
        "amount_with_tax": 113.0, "tax_amount": 13.0, "tax_rate": 0.13,
    })
    check("规范发票无问题", good == [], str(field_names(good)))

    mismatch = invoice_issues({
        "invoice_number": "24322000000000000001", "invoice_date": "2025-03-01",
        "seller_name": "测试商户", "amount_with_tax": 1130, "tax_amount": 500, "tax_rate": 0.13,
    })
    check("税额与税率不符被抓出",
          any(i["field"] == "tax_amount" and "对不上" in i["message"] for i in mismatch),
          str(field_names(mismatch)))

    future = invoice_issues({
        "invoice_number": "12345678", "invoice_date": "2099-01-01",
        "seller_name": "测试商户", "amount_with_tax": 10, "tax_amount": 0,
    })
    check("未来日期被抓出",
          any(i["field"] == "invoice_date" for i in future), str(field_names(future)))

    no_tax_num = invoice_issues({
        "invoice_number": "12345678", "invoice_date": "2025-01-01",
        "seller_name": "测试", "buyer_name": "中国矿业大学",
        "amount_with_tax": 10, "tax_amount": 0,
    })
    check("有购买方无税号被抓出",
          any(i["field"] == "buyer_tax_num" for i in no_tax_num), str(field_names(no_tax_num)))

    ordered = invoice_issues({"invoice_number": "", "amount_with_tax": 0, "invoice_date": "",
                              "seller_name": ""})
    levels = [i["level"] for i in ordered]
    check("error 排在 warn 前面", levels == sorted(levels, key=lambda x: 0 if x == "error" else 1),
          str(levels))


# ── 2. 金额筛选解析 ──────────────────────────────
def test_amount_filter():
    from invoice_clipper.web import parse_amount_filter

    cases = [
        ("", (None, None)),
        ("100", (10000, 10000)),
        (">=100", (10000, None)),
        ("<=100", (None, 10000)),
        (">100", (10001, None)),
        ("<100", (None, 9999)),
        ("100-200", (10000, 20000)),
        ("200-100", (10000, 20000)),
        ("¥1,234.56", (123456, 123456)),
    ]
    for raw, expected in cases:
        try:
            got = parse_amount_filter(raw)
        except Exception as exc:  # noqa: BLE001
            check(f"金额解析 {raw!r}", False, f"抛异常 {exc}")
            continue
        check(f"金额解析 {raw!r}", got == expected, f"期望 {expected} 得到 {got}")

    for bad in ("abc", "-", "100-", ">="):
        try:
            parse_amount_filter(bad)
        except (ValueError, TypeError, ArithmeticError):
            continue
        check(f"非法金额 {bad!r} 应报错", False)


# ── 3. 凭据环境变量覆盖 ──────────────────────
def test_env_overrides():
    import copy
    from invoice_clipper import DEFAULT_CONFIG, apply_env_overrides

    base = copy.deepcopy(DEFAULT_CONFIG)
    base["ocr"].setdefault("baidu", {})["api_key"] = "yaml-key"

    got = apply_env_overrides(copy.deepcopy(base), {"INVOICE_BAIDU_API_KEY": "env-key"})
    check("环境变量覆盖 yaml", got["ocr"]["baidu"]["api_key"] == "env-key",
          got["ocr"]["baidu"]["api_key"])

    got = apply_env_overrides(copy.deepcopy(base), {"INVOICE_BAIDU_API_KEY": "   "})
    check("空值不覆盖", got["ocr"]["baidu"]["api_key"] == "yaml-key",
          got["ocr"]["baidu"]["api_key"])

    tmp = Path(tempfile.mkdtemp()) / "secret.txt"
    tmp.write_text("file-key\n", encoding="utf-8")
    got = apply_env_overrides(copy.deepcopy(base), {"INVOICE_BAIDU_API_KEY_FILE": str(tmp)})
    check("_FILE 变体读取", got["ocr"]["baidu"]["api_key"] == "file-key",
          got["ocr"]["baidu"]["api_key"])

    got = apply_env_overrides(copy.deepcopy(base), {"INVOICE_BAIDU_API_KEY_FILE": "/tmp/not-here"})
    check("文件缺失时保留原值", got["ocr"]["baidu"]["api_key"] == "yaml-key",
          got["ocr"]["baidu"]["api_key"])


# ── 4. 按分取整 ──────────────────────────────────
def test_cents():
    check("0.1+0.2 取分", amount_cents(0.30000000000000004) == 30)
    check("字符串金额", amount_cents("137.50") == 13750)
    check("空值", amount_cents(None) == 0)
    check("四舍五入", amount_cents("1.005") in (100, 101))


# ── 4. 报销单流程 ────────────────────────────────
def test_reimbursement():
    import invoice_clipper as ic
    from invoice_clipper.workflow import (
        add_invoices_to_reimbursement, get_reimbursement,
        record_reimbursement, remove_invoices_from_reimbursement, void_reimbursement,
    )

    tmp = Path(tempfile.mkdtemp())
    cfg = {"storage": {"db_type": "sqlite", "db_path": str(tmp / "t.db")}}
    ic.init_db(cfg)

    ids = [ic.insert_invoice({
        "invoice_number": f"T{i}", "seller_name": f"商户{i}", "invoice_date": "2025-05-01",
        "amount_with_tax": 100.0 + i, "status": "ready",
    }) for i in range(3)]

    out = tmp / "报销明细_t.xlsx"
    out.write_bytes(b"x")
    # 前两张进报销单，第三张留在可报销池里等后面补充
    batch_id = record_reimbursement([ic.get_invoice_by_id(i) for i in ids[:2]], [out],
                                    mark_reported=True)
    batch = get_reimbursement(batch_id)
    check("创建后状态与合计", batch["status"] == "reported" and batch["total_cents"] == 20100,
          f"{batch['status']} {batch['total_cents']}")

    remove_invoices_from_reimbursement(batch_id, [ids[1]], "ready")
    batch = get_reimbursement(batch_id)
    check("移出后合计重算", batch["total_cents"] == 10000, str(batch["total_cents"]))
    check("移出的发票回到可报销", ic.get_invoice_by_id(ids[1])["status"] == "ready")
    check("剩余发票仍为已报销", ic.get_invoice_by_id(ids[0])["status"] == "reported")

    add_invoices_to_reimbursement(batch_id, [ids[2]])
    batch = get_reimbursement(batch_id)
    check("补充后合计", batch["total_cents"] == 20200, str(batch["total_cents"]))
    check("已报销单据补充的票同步状态", ic.get_invoice_by_id(ids[2])["status"] == "reported")

    void_reimbursement(batch_id)
    batch = get_reimbursement(batch_id)
    check("整单退回后作废", batch["status"] == "voided" and batch["total_cents"] == 0,
          f"{batch['status']} {batch['total_cents']}")
    check("整单退回后发票全部回到可报销",
          all(ic.get_invoice_by_id(i)["status"] == "ready" for i in ids))


def main():
    for fn in (test_checks, test_amount_filter, test_env_overrides, test_cents, test_reimbursement):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{fn.__name__} 抛出异常: {exc}")

    if failures:
        print(f"✗ {len(failures)} 项未通过：")
        for item in failures:
            print("  -", item)
        return 1
    print("✓ 全部自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
