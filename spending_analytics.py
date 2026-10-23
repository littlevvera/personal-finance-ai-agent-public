"""Monthly spending analytics. All money math is integer cents. No LLM."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

import pandas as pd

from reconciliation import (
    CARD_PAYMENT,
    EXCEPTION_STATUSES,
    POSTED_CHARGE_STATUSES,
    TRANSFER_STATUSES,
    UNMATCHED,
    actual_posted_spend_cents,
    cents_to_dollars,
    duplicate_exposure_cents,
    merchant_key,
    normalize_banks,
    normalize_cards,
    normalize_orders,
    to_cents,
)

CATEGORIES = [
    "groceries",
    "household",
    "baby and family",
    "shopping",
    "dining",
    "transportation",
    "entertainment",
    "subscriptions",
    "healthcare",
    "travel",
    "other",
    "uncategorized",
]

UNCATEGORIZED = "uncategorized"

ITEM_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("baby and family", ("diaper", "wipe", "formula", "baby", "stroller")),
    ("healthcare", ("pharmacy", "rx ", "vitamin", "clinic", "medicine", "bandage")),
    ("household", ("towel", "batter", "cleaner", "detergent", "paper towel", "trash", "soap")),
    (
        "groceries",
        (
            "coffee",
            "olive oil",
            "spice",
            "banana",
            "milk",
            "loaf",
            "egg",
            "chicken",
            "salad",
            "oat",
            "water",
            "grocery",
            "produce",
            "bread",
            "rotisserie",
        ),
    ),
    ("dining", ("restaurant", "diner", "cafe", "coffee shop", "doordash", "ubereats")),
    ("transportation", ("uber", "lyft", "gas station", "fuel", "parking", "transit")),
    ("entertainment", ("netflix", "spotify", "movie", "theater", "game")),
    ("subscriptions", ("subscription", "membership")),
    ("travel", ("hotel", "airline", "airbnb", "flight")),
    (
        "shopping",
        (
            "hdmi",
            "cable",
            "mouse",
            "keyboard",
            "skillet",
            "case",
            "pad",
            "electronics",
        ),
    ),
]

MERCHANT_FALLBACK = {
    "wholefoods": "groceries",
    "amazon": "shopping",
}


def _percent(part_cents: int, whole_cents: int) -> float:
    if whole_cents <= 0:
        return 0.0
    return float((Decimal(part_cents) * Decimal(100) / Decimal(whole_cents)).quantize(Decimal("0.01")))


def _blob(*parts: object) -> str:
    return " ".join(str(part or "") for part in parts).lower()


def categorize_item(description: object, extra: object = "") -> str:
    text = _blob(description, extra)
    if not text.strip():
        return UNCATEGORIZED
    for category, needles in ITEM_RULES:
        if any(needle in text for needle in needles):
            return category
    return UNCATEGORIZED


def categorize_merchant(merchant: object, description: object = "") -> str:
    key = merchant_key(f"{merchant} {description}")
    if key in MERCHANT_FALLBACK:
        return MERCHANT_FALLBACK[key]
    if key == "costco":
        return UNCATEGORIZED
    if key in {"", "unknown", "card_payment"}:
        return UNCATEGORIZED
    itemish = categorize_item(merchant, description)
    if itemish != UNCATEGORIZED:
        return itemish
    return UNCATEGORIZED


def _allocate(weights: list[tuple[str, int]], total_cents: int) -> dict[str, int]:
    """Split total_cents across categories using positive weights. Sums to total_cents."""
    buckets: dict[str, int] = defaultdict(int)
    if total_cents <= 0:
        return {}
    collapsed: dict[str, int] = defaultdict(int)
    for cat, weight in weights:
        if cat:
            collapsed[cat] += max(int(weight), 0)
    positive = list(collapsed.items())
    weight_sum = sum(weight for _, weight in positive)
    if weight_sum <= 0:
        buckets[UNCATEGORIZED] = total_cents
        return dict(buckets)
    assigned = 0
    ranked = sorted(positive, key=lambda item: (-item[1], item[0]))
    for index, (cat, weight) in enumerate(ranked):
        if index == len(ranked) - 1:
            share = total_cents - assigned
        else:
            share = int((Decimal(total_cents) * Decimal(weight) / Decimal(weight_sum)).quantize(Decimal("1")))
            assigned += share
        if share:
            buckets[cat] += share
    leftover = total_cents - sum(buckets.values())
    if leftover:
        buckets[ranked[0][0]] += leftover
    return dict(buckets)


def _order_lines(orders_df: Optional[pd.DataFrame], order_id: str) -> list[dict]:
    if orders_df is None or orders_df.empty or not order_id:
        return []
    id_col = None
    lower = {str(col).strip().lower(): col for col in orders_df.columns}
    for name in ("order_id", "id"):
        if name in lower:
            id_col = lower[name]
            break
    if not id_col:
        return []
    desc_col = lower.get("item_description") or lower.get("description")
    amount_col = lower.get("line_total") or lower.get("unit_price")
    rows = []
    for _, raw in orders_df.loc[orders_df[id_col].astype(str) == str(order_id)].iterrows():
        desc = str(raw[desc_col]) if desc_col else ""
        amount = to_cents(raw[amount_col]) if amount_col else 0
        rows.append({"description": desc, "amount_cents": amount})
    return rows


def _receipt_lines(receipts: Iterable[dict], order_id: str, txn_id: str) -> list[dict]:
    lines = []
    for receipt in receipts or []:
        if not isinstance(receipt, dict) or not receipt.get("confirmed"):
            continue
        source = str(receipt.get("source_id") or "")
        rec_order = str(receipt.get("order_id") or "")
        filename = str(receipt.get("source_filename") or "")
        tied = (
            (order_id and rec_order and rec_order == order_id)
            or (txn_id and txn_id in source)
            or False
        )
        if not tied and order_id and order_id in " ".join([rec_order, filename, source]):
            tied = True
        if not tied:
            continue
        for item in receipt.get("items") or []:
            if not isinstance(item, dict):
                continue
            lines.append(
                {
                    "description": item.get("description") or "",
                    "amount_cents": to_cents(item.get("amount")) if item.get("amount") is not None else 0,
                }
            )
    return lines


def _receipt_for_unmatched(receipts: Iterable[dict], evidence_ids: str, merchant: str) -> list[dict]:
    evidence = evidence_ids or ""
    lines = []
    for receipt in receipts or []:
        if not isinstance(receipt, dict) or not receipt.get("confirmed"):
            continue
        source = str(receipt.get("source_id") or f"receipt:{receipt.get('source_filename')}")
        if source not in evidence and str(receipt.get("source_filename") or "") not in evidence:
            continue
        for item in receipt.get("items") or []:
            if not isinstance(item, dict):
                continue
            lines.append(
                {
                    "description": item.get("description") or "",
                    "amount_cents": to_cents(item.get("amount")) if item.get("amount") is not None else 0,
                }
            )
        if lines:
            return lines
    return lines


def _lines_to_weights(lines: list[dict]) -> list[tuple[str, int]]:
    weights: list[tuple[str, int]] = []
    for line in lines:
        cat = categorize_item(line.get("description"))
        amount = int(line.get("amount_cents") or 0)
        if amount < 0:
            continue
        weights.append((cat, amount if amount else 1))
    return weights


def _card_lookup(card_df: Optional[pd.DataFrame]) -> dict[str, dict]:
    cards = normalize_cards(card_df if card_df is not None else pd.DataFrame())
    return {
        txn.txn_id: {
            "posted_date": txn.posted_date,
            "merchant_raw": txn.merchant_raw,
            "merchant_key": txn.merchant_key,
            "txn_type": txn.txn_type,
            "amount_cents": txn.amount_cents,
            "description": txn.merchant_raw,
        }
        for txn in cards
    }


def _is_bank_expense(bank) -> bool:
    if bank.txn_type in {"deposit", "transfer", "payment"}:
        return False
    key = merchant_key(bank.description)
    if key == "card_payment":
        return False
    text = bank.description.lower()
    if "autopay" in text or "credit crd" in text or "savings" in text:
        return False
    return bank.txn_type in {"purchase", "withdrawal", "debit", "ach", "pos", "check"}


def _period_label(dates: list[Optional[date]]) -> str:
    present = [item for item in dates if item]
    if not present:
        return "unknown"
    months = sorted({item.strftime("%Y-%m") for item in present})
    if len(months) == 1:
        return months[0]
    return f"{months[0]} to {months[-1]}"


def analyze_spending(
    recon_df: pd.DataFrame,
    card_df: Optional[pd.DataFrame] = None,
    orders_df: Optional[pd.DataFrame] = None,
    bank_df: Optional[pd.DataFrame] = None,
    confirmed_receipts: Optional[list[dict]] = None,
) -> dict:
    receipts = confirmed_receipts or []
    card_info = _card_lookup(card_df)
    cards = normalize_cards(card_df if card_df is not None else pd.DataFrame())
    banks = normalize_banks(bank_df)
    orders = normalize_orders(orders_df if orders_df is not None else pd.DataFrame())
    orders_by_id = {order.order_id: order for order in orders}

    posted_refund_cents = sum(txn.amount_cents for txn in cards if txn.txn_type == "refund")
    card_ids = {txn.txn_id for txn in cards}

    detail_rows: list[dict] = []
    category_cents: dict[str, int] = defaultdict(int)
    merchant_cents: dict[str, int] = defaultdict(int)
    merchant_txns: dict[str, int] = defaultdict(int)
    merchant_label: dict[str, str] = {}
    used_bank: set[str] = set()
    warnings: list[str] = []

    if recon_df is None or recon_df.empty:
        warnings.append("No reconciliation result is available. Spending cannot be computed.")
        return _empty_report(warnings)

    seen_txn: set[str] = set()
    for _, row in recon_df.iterrows():
        txn_id = str(row.get("transaction_id") or "")
        status = str(row.get("reconciliation_status") or "")
        if not txn_id or txn_id in seen_txn:
            continue
        if status in TRANSFER_STATUSES:
            seen_txn.add(txn_id)
            continue
        if status not in POSTED_CHARGE_STATUSES:
            continue
        seen_txn.add(txn_id)
        amount_cents = to_cents(row.get("transaction_amount"))
        info = card_info.get(txn_id, {})
        merchant = str(row.get("merchant") or info.get("merchant_raw") or "")
        key = info.get("merchant_key") or merchant_key(merchant)
        order_id = str(row.get("order_id") or "")
        evidence = str(row.get("evidence_ids") or "")
        posted_date = info.get("posted_date")

        lines = _order_lines(orders_df, order_id)
        if not lines:
            lines = _receipt_for_unmatched(receipts, evidence, merchant)
        if not lines:
            lines = _receipt_lines(receipts, order_id, txn_id)

        if lines:
            weights = _lines_to_weights(lines)
            split = _allocate(weights, amount_cents)
        else:
            fallback = categorize_merchant(merchant, info.get("description"))
            if key == "costco":
                warnings.append(
                    f"{txn_id}: Costco has no item lines, so the amount is uncategorized instead of assuming groceries."
                )
            elif fallback == UNCATEGORIZED:
                warnings.append(
                    f"{txn_id}: no reliable item or merchant category; amount is uncategorized."
                )
            split = {fallback: amount_cents}

        for cat, cents in split.items():
            category_cents[cat] += cents
        merchant_cents[key] += amount_cents
        merchant_txns[key] += 1
        merchant_label[key] = merchant or key
        cats = [cat for cat, cents in sorted(split.items(), key=lambda item: -item[1]) if cents]
        detail_rows.append(
            {
                "transaction_id": txn_id,
                "transaction_date": posted_date.isoformat() if posted_date else "",
                "merchant": merchant,
                "merchant_key": key,
                "amount": cents_to_dollars(amount_cents),
                "amount_cents": amount_cents,
                "spending_category": ", ".join(cats) if cats else UNCATEGORIZED,
                "reconciliation_status": status,
                "supporting_source_ids": evidence,
                "order_id": order_id,
            }
        )

    for bank in banks:
        if bank.txn_id in used_bank:
            continue
        if bank.related_txn_id and bank.related_txn_id in card_ids:
            continue
        if not _is_bank_expense(bank):
            continue
        used_bank.add(bank.txn_id)
        amount_cents = bank.amount_cents
        key = merchant_key(bank.description)
        fallback = categorize_merchant(bank.description)
        category_cents[fallback] += amount_cents
        merchant_cents[key] += amount_cents
        merchant_txns[key] += 1
        merchant_label[key] = bank.description or key
        detail_rows.append(
            {
                "transaction_id": bank.txn_id,
                "transaction_date": bank.posted_date.isoformat() if bank.posted_date else "",
                "merchant": bank.description,
                "merchant_key": key,
                "amount": cents_to_dollars(amount_cents),
                "amount_cents": amount_cents,
                "spending_category": fallback,
                "reconciliation_status": "BANK_EXPENSE",
                "supporting_source_ids": bank.txn_id,
                "order_id": "",
            }
        )

    posted_cents = actual_posted_spend_cents(recon_df) + sum(
        to_cents(row["amount"]) for row in detail_rows if row["reconciliation_status"] == "BANK_EXPENSE"
    )
    duplicate_cents = duplicate_exposure_cents(recon_df)
    net_cents = posted_cents - posted_refund_cents
    category_sum = sum(category_cents.values())
    unallocated_cents = posted_cents - category_sum
    if unallocated_cents:
        category_cents[UNCATEGORIZED] += unallocated_cents
        warnings.append(
            f"Category totals were {cents_to_dollars(category_sum):.2f} versus posted "
            f"{cents_to_dollars(posted_cents):.2f}; {cents_to_dollars(unallocated_cents):.2f} is unallocated."
        )

    if posted_refund_cents:
        warnings.append(
            f"Posted card refunds of {cents_to_dollars(posted_refund_cents):.2f} reduce net spending "
            "and are not assigned to purchase categories."
        )

    unverified_refund = recon_df["reconciliation_status"].eq("REFUND_NOT_FOUND").any() if not recon_df.empty else False
    if unverified_refund:
        warnings.append(
            "A refund appears on an order but no posted card refund was found. "
            "That pending refund is not subtracted from net spending."
        )

    dates = []
    for row in detail_rows:
        if row["transaction_date"]:
            dates.append(date.fromisoformat(row["transaction_date"]))
    period = _period_label(dates)
    if " to " in period:
        warnings.append(
            f"Spending covers more than one month ({period}). Totals are not a single-month series."
        )

    denom = posted_cents if posted_cents else 1
    categories = []
    for name in CATEGORIES:
        cents = int(category_cents.get(name, 0))
        if cents == 0 and name not in category_cents:
            continue
        categories.append(
            {
                "category": name,
                "amount_cents": cents,
                "amount": cents_to_dollars(cents),
                "percent": _percent(cents, posted_cents),
            }
        )
    for name, cents in sorted(category_cents.items()):
        if name not in CATEGORIES and cents:
            categories.append(
                {
                    "category": name,
                    "amount_cents": cents,
                    "amount": cents_to_dollars(cents),
                    "percent": _percent(cents, posted_cents),
                }
            )
    categories.sort(key=lambda item: (-item["amount_cents"], item["category"]))

    merchants = []
    for key, cents in merchant_cents.items():
        merchants.append(
            {
                "merchant_key": key,
                "merchant": merchant_label.get(key, key),
                "amount_cents": cents,
                "amount": cents_to_dollars(cents),
                "percent": _percent(cents, posted_cents),
                "transaction_count": merchant_txns[key],
            }
        )
    merchants.sort(key=lambda item: (-item["amount_cents"], item["merchant"]))
    top_merchants = merchants[:5]

    unmatched_count = int(
        recon_df["reconciliation_status"].eq(UNMATCHED).eq(True).sum()
        if not recon_df.empty
        else 0
    )
    unmatched_posted = [
        row
        for row in detail_rows
        if row["reconciliation_status"] == UNMATCHED
    ]
    exception_count = int(recon_df["reconciliation_status"].isin(EXCEPTION_STATUSES).sum()) if not recon_df.empty else 0
    uncategorized_cents = int(category_cents.get(UNCATEGORIZED, 0))

    checks = _quality_checks(
        recon_df=recon_df,
        posted_cents=posted_cents,
        net_cents=net_cents,
        refund_cents=posted_refund_cents,
        duplicate_cents=duplicate_cents,
        category_cents=dict(category_cents),
        categories=categories,
        detail_rows=detail_rows,
        banks=banks,
        cards=cards,
        unmatched_posted=unmatched_posted,
    )
    warnings.extend(item["message"] for item in checks if not item["ok"])

    top_category = categories[0]["category"] if categories and posted_cents else None
    top_merchant = top_merchants[0]["merchant"] if top_merchants else None

    return {
        "period": period,
        "posted_spending_cents": posted_cents,
        "posted_spending": cents_to_dollars(posted_cents),
        "net_spending_cents": net_cents,
        "net_spending": cents_to_dollars(net_cents),
        "posted_refunds_cents": posted_refund_cents,
        "posted_refunds": cents_to_dollars(posted_refund_cents),
        "duplicate_exposure_cents": duplicate_cents,
        "duplicate_exposure": cents_to_dollars(duplicate_cents),
        "transactions_analyzed": int(len(cards)),
        "unmatched_transactions": int(len(unmatched_posted)),
        "unmatched_recon_rows": unmatched_count,
        "exceptions_requiring_review": exception_count,
        "uncategorized_cents": uncategorized_cents,
        "uncategorized": cents_to_dollars(uncategorized_cents),
        "uncategorized_percent": _percent(uncategorized_cents, posted_cents),
        "categories": categories,
        "top_merchants": top_merchants,
        "merchants": merchants,
        "transactions": detail_rows,
        "top_category": top_category,
        "top_merchant": top_merchant,
        "warnings": warnings,
        "checks": checks,
        "orders_by_id": list(orders_by_id),
        "denominator": denom,
    }


def _empty_report(warnings: list[str]) -> dict:
    return {
        "period": "unknown",
        "posted_spending_cents": 0,
        "posted_spending": 0.0,
        "net_spending_cents": 0,
        "net_spending": 0.0,
        "posted_refunds_cents": 0,
        "posted_refunds": 0.0,
        "duplicate_exposure_cents": 0,
        "duplicate_exposure": 0.0,
        "transactions_analyzed": 0,
        "unmatched_transactions": 0,
        "unmatched_recon_rows": 0,
        "exceptions_requiring_review": 0,
        "uncategorized_cents": 0,
        "uncategorized": 0.0,
        "uncategorized_percent": 0.0,
        "categories": [],
        "top_merchants": [],
        "merchants": [],
        "transactions": [],
        "top_category": None,
        "top_merchant": None,
        "warnings": warnings,
        "checks": [],
        "orders_by_id": [],
        "denominator": 1,
    }


def _quality_checks(
    *,
    recon_df: pd.DataFrame,
    posted_cents: int,
    net_cents: int,
    refund_cents: int,
    duplicate_cents: int,
    category_cents: dict[str, int],
    categories: list[dict],
    detail_rows: list[dict],
    banks,
    cards,
    unmatched_posted: list[dict],
) -> list[dict]:
    checks = []

    cat_sum = sum(category_cents.values())
    checks.append(
        {
            "code": "category_totals_match_posted",
            "ok": cat_sum == posted_cents,
            "message": (
                "Category totals match posted spending."
                if cat_sum == posted_cents
                else f"Category totals {cents_to_dollars(cat_sum):.2f} do not match posted {cents_to_dollars(posted_cents):.2f}."
            ),
        }
    )
    net_expected = posted_cents - refund_cents
    checks.append(
        {
            "code": "net_equals_posted_minus_refunds",
            "ok": net_cents == net_expected,
            "message": (
                "Net spending equals posted spending minus posted refunds."
                if net_cents == net_expected
                else "Net spending does not equal posted spending minus posted refunds."
            ),
        }
    )
    if refund_cents == 0:
        checks.append(
            {
                "code": "categories_vs_net",
                "ok": cat_sum == net_cents,
                "message": "With no posted refunds, category totals equal net spending.",
            }
        )
    else:
        checks.append(
            {
                "code": "categories_vs_net",
                "ok": True,
                "message": (
                    "Posted refunds reduce net spending and are shown separately; "
                    "category totals follow posted purchase amounts."
                ),
            }
        )

    percent_sum = sum((Decimal(str(item["percent"])) for item in categories), Decimal("0"))
    percent_ok = not categories or abs(percent_sum - Decimal("100")) <= Decimal("0.10")
    checks.append(
        {
            "code": "percentages_near_100",
            "ok": percent_ok,
            "message": (
                f"Category percentages sum to {percent_sum}."
                if percent_ok
                else f"Category percentages sum to {percent_sum}, not ~100."
            ),
        }
    )

    payment_in_spend = any(
        row["reconciliation_status"] == CARD_PAYMENT for row in detail_rows
    )
    checks.append(
        {
            "code": "card_payments_excluded",
            "ok": not payment_in_spend,
            "message": (
                "Credit card payments are excluded from spending."
                if not payment_in_spend
                else "A CARD_PAYMENT row was included in spending."
            ),
        }
    )

    bank_transfers_in_spend = [
        bank.txn_id
        for bank in banks
        if bank.txn_type == "transfer"
        and any(row["transaction_id"] == bank.txn_id for row in detail_rows)
    ]
    checks.append(
        {
            "code": "transfers_excluded",
            "ok": not bank_transfers_in_spend,
            "message": (
                "Internal bank transfers are excluded from spending."
                if not bank_transfers_in_spend
                else f"Bank transfer(s) included in spending: {', '.join(bank_transfers_in_spend)}."
            ),
        }
    )

    duplicate_rows = [row for row in detail_rows if row["reconciliation_status"] == "POSSIBLE_DUPLICATE"]
    dup_included = sum(row["amount_cents"] for row in duplicate_rows) == duplicate_cents or duplicate_cents == 0
    checks.append(
        {
            "code": "duplicates_in_posted",
            "ok": dup_included,
            "message": (
                "Suspected duplicate charges remain in posted spending and are listed separately as exposure."
                if dup_included
                else "Suspected duplicate exposure does not match duplicate rows in the spend table."
            ),
        }
    )

    recon_unmatched_txn = set(
        str(val)
        for val in recon_df.loc[
            recon_df["reconciliation_status"].eq(UNMATCHED) & recon_df["transaction_id"].ne(""),
            "transaction_id",
        ].tolist()
    ) if not recon_df.empty else set()
    detail_unmatched = {row["transaction_id"] for row in unmatched_posted}
    checks.append(
        {
            "code": "unmatched_posted_included",
            "ok": recon_unmatched_txn <= detail_unmatched,
            "message": (
                "Unmatched posted purchases are included in spending."
                if recon_unmatched_txn <= detail_unmatched
                else "An unmatched posted purchase is missing from spending."
            ),
        }
    )

    checks.append(
        {
            "code": "uncategorized_visible",
            "ok": True,
            "message": (
                f"Uncategorized spending is {cents_to_dollars(category_cents.get(UNCATEGORIZED, 0)):.2f} "
                "and remains a visible category."
            ),
        }
    )

    refund_types = [txn.txn_id for txn in cards if txn.txn_type == "refund"]
    checks.append(
        {
            "code": "posted_refunds_only",
            "ok": True,
            "message": (
                f"{len(refund_types)} posted card refund(s) subtracted from net spending."
                if refund_types
                else "No posted card refunds. Unverified order refunds were not subtracted."
            ),
        }
    )
    return checks


def insight_payload(report: dict) -> dict:
    """Compact verified aggregates for the insights model. No raw statements."""
    return {
        "period": report.get("period"),
        "posted_spending": report.get("posted_spending"),
        "net_spending": report.get("net_spending"),
        "posted_refunds": report.get("posted_refunds"),
        "duplicate_exposure": report.get("duplicate_exposure"),
        "uncategorized": report.get("uncategorized"),
        "uncategorized_percent": report.get("uncategorized_percent"),
        "transactions_analyzed": report.get("transactions_analyzed"),
        "unmatched_transactions": report.get("unmatched_transactions"),
        "exceptions_requiring_review": report.get("exceptions_requiring_review"),
        "top_category": report.get("top_category"),
        "top_merchant": report.get("top_merchant"),
        "categories": [
            {
                "category": item["category"],
                "amount": item["amount"],
                "percent": item["percent"],
            }
            for item in report.get("categories") or []
        ],
        "top_merchants": [
            {
                "merchant": item["merchant"],
                "amount": item["amount"],
                "percent": item["percent"],
                "transaction_count": item["transaction_count"],
            }
            for item in report.get("top_merchants") or []
        ],
        "exception_summaries": [
            {
                "transaction_id": row["transaction_id"],
                "status": row["reconciliation_status"],
                "merchant": row["merchant"],
                "amount": row["amount"],
            }
            for row in report.get("transactions") or []
            if row["reconciliation_status"] in EXCEPTION_STATUSES
        ],
        "warnings": report.get("warnings") or [],
        "limitations": [
            "Only one reporting period is available; do not claim month-over-month changes.",
            "Orders and receipts are evidence, not extra spend.",
            "Pending refunds that did not post to the card are not subtracted.",
        ],
    }
