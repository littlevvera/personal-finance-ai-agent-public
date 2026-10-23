"""Deterministic card/order/bank reconciliation. No LLM. Money is integer cents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_EVEN
from itertools import combinations
from typing import Iterable, Optional

import pandas as pd

MATCHED = "MATCHED"
MATCHED_SPLIT = "MATCHED_SPLIT"
POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
REFUND_NOT_FOUND = "REFUND_NOT_FOUND"
UNMATCHED = "UNMATCHED"
NEEDS_REVIEW = "NEEDS_REVIEW"
CARD_PAYMENT = "CARD_PAYMENT"

MATCHED_STATUSES = {MATCHED, MATCHED_SPLIT}
TRANSFER_STATUSES = {CARD_PAYMENT}
EXCEPTION_STATUSES = {
    POSSIBLE_DUPLICATE,
    AMOUNT_MISMATCH,
    REFUND_NOT_FOUND,
    UNMATCHED,
    NEEDS_REVIEW,
}
POSTED_CHARGE_STATUSES = {
    MATCHED,
    MATCHED_SPLIT,
    AMOUNT_MISMATCH,
    REFUND_NOT_FOUND,
    UNMATCHED,
    POSSIBLE_DUPLICATE,
    NEEDS_REVIEW,
}

DATE_WINDOW_DAYS = 7
DUPLICATE_WINDOW_DAYS = 1
OUTPUT_COLUMNS = [
    "exception_id",
    "transaction_id",
    "order_id",
    "match_group_id",
    "merchant",
    "transaction_amount",
    "order_amount",
    "difference",
    "group_transaction_amount",
    "group_order_amount",
    "group_difference",
    "reconciliation_status",
    "evidence_ids",
    "explanation",
]


def to_cents(value) -> int:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0
    if isinstance(value, str) and not value.strip():
        return 0
    quantized = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return int(quantized * 100)


def cents_to_dollars(cents: int) -> float:
    return float(Decimal(cents) / Decimal(100))


def _first_present(frame: pd.DataFrame, candidates: Iterable[str]) -> Optional[str]:
    lower = {str(col).strip().lower(): col for col in frame.columns}
    for name in candidates:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def _parse_date(value) -> Optional[date]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def merchant_key(text: object) -> str:
    raw = "" if text is None or (isinstance(text, float) and pd.isna(text)) else str(text)
    compact = "".join(ch for ch in raw.lower() if ch.isalnum())
    if "amzn" in compact or "amazon" in compact:
        return "amazon"
    if "costco" in compact:
        return "costco"
    if "wholefds" in compact or "wholefoods" in compact:
        return "wholefoods"
    if "payment" in compact or "autopay" in compact:
        return "card_payment"
    return compact or "unknown"


def _within(a: Optional[date], b: Optional[date], days: int) -> bool:
    if a is None or b is None:
        return False
    return abs((a - b).days) <= days


@dataclass
class CardTxn:
    txn_id: str
    posted_date: Optional[date]
    merchant_raw: str
    merchant_key: str
    txn_type: str
    amount_cents: int
    reference: str
    related_order_id: str


@dataclass
class Order:
    order_id: str
    order_date: Optional[date]
    merchant_raw: str
    merchant_key: str
    total_cents: int
    refund_cents: int
    refund_date: Optional[date]
    status: str
    shipment_dates: list = field(default_factory=list)
    external_order_number: str = ""


@dataclass
class BankTxn:
    txn_id: str
    posted_date: Optional[date]
    description: str
    txn_type: str
    amount_cents: int
    related_txn_id: str


def normalize_cards(frame: pd.DataFrame) -> list[CardTxn]:
    if frame is None or frame.empty:
        return []
    id_col = _first_present(frame, ["txn_id", "transaction_id", "id"])
    date_col = _first_present(frame, ["posted_date", "transaction_date", "date"])
    amount_col = _first_present(frame, ["amount", "transaction_amount"])
    type_col = _first_present(frame, ["txn_type", "type", "transaction_type"])
    merchant_col = _first_present(frame, ["merchant_raw", "merchant", "description"])
    desc_col = _first_present(frame, ["description", "merchant_raw"])
    ref_col = _first_present(frame, ["reference", "ref"])
    order_col = _first_present(frame, ["related_order_id", "order_id"])
    if not id_col or not amount_col:
        raise ValueError("Credit card file needs an id column and an amount column.")

    rows = []
    for _, raw in frame.iterrows():
        amount = to_cents(raw[amount_col])
        signed = amount
        raw_amount = raw[amount_col]
        try:
            if Decimal(str(raw_amount)) < 0:
                signed = -abs(amount)
                amount = abs(amount)
        except Exception:
            amount = abs(amount)
        txn_type = str(raw[type_col]).strip().lower() if type_col else "charge"
        merchant = str(raw[merchant_col]) if merchant_col else ""
        description = str(raw[desc_col]) if desc_col else merchant
        key = merchant_key(f"{merchant} {description}")
        if key == "card_payment" and txn_type not in {"refund"}:
            txn_type = "payment"
        if signed < 0 and txn_type == "charge":
            txn_type = "refund"
        related = ""
        if order_col and not pd.isna(raw[order_col]):
            related = str(raw[order_col]).strip()
            if related.lower() in {"", "nan", "none"}:
                related = ""
        rows.append(
            CardTxn(
                txn_id=str(raw[id_col]),
                posted_date=_parse_date(raw[date_col]) if date_col else None,
                merchant_raw=merchant or description,
                merchant_key=key,
                txn_type=txn_type,
                amount_cents=amount,
                reference=str(raw[ref_col]) if ref_col and not pd.isna(raw[ref_col]) else "",
                related_order_id=related,
            )
        )
    return rows


def normalize_orders(frame: pd.DataFrame) -> list[Order]:
    if frame is None or frame.empty:
        return []
    id_col = _first_present(frame, ["order_id", "id"])
    date_col = _first_present(frame, ["order_date", "date"])
    merchant_col = _first_present(
        frame, ["merchant_normalized", "merchant", "store"]
    )
    total_col = _first_present(frame, ["order_total", "total", "amount"])
    refund_col = _first_present(frame, ["refund_amount"])
    refund_date_col = _first_present(frame, ["refund_date"])
    status_col = _first_present(frame, ["order_status", "status"])
    ship_date_col = _first_present(frame, ["shipment_date"])
    ext_col = _first_present(frame, ["external_order_number"])
    if not id_col or not total_col:
        raise ValueError("Orders file needs an order id column and an order total column.")

    grouped: dict[str, Order] = {}
    for _, raw in frame.iterrows():
        order_id = str(raw[id_col])
        if order_id not in grouped:
            grouped[order_id] = Order(
                order_id=order_id,
                order_date=_parse_date(raw[date_col]) if date_col else None,
                merchant_raw=str(raw[merchant_col]) if merchant_col else "",
                merchant_key=merchant_key(raw[merchant_col] if merchant_col else ""),
                total_cents=to_cents(raw[total_col]),
                refund_cents=to_cents(raw[refund_col]) if refund_col else 0,
                refund_date=_parse_date(raw[refund_date_col]) if refund_date_col else None,
                status=str(raw[status_col]).strip().lower() if status_col else "",
                external_order_number=str(raw[ext_col]) if ext_col and not pd.isna(raw[ext_col]) else "",
            )
        ship = _parse_date(raw[ship_date_col]) if ship_date_col else None
        if ship and ship not in grouped[order_id].shipment_dates:
            grouped[order_id].shipment_dates.append(ship)
    return list(grouped.values())


def normalize_banks(frame: Optional[pd.DataFrame]) -> list[BankTxn]:
    if frame is None or frame.empty:
        return []
    id_col = _first_present(frame, ["txn_id", "transaction_id", "id"])
    date_col = _first_present(frame, ["posted_date", "date"])
    amount_col = _first_present(frame, ["amount"])
    type_col = _first_present(frame, ["txn_type", "type"])
    desc_col = _first_present(frame, ["description", "counterparty"])
    related_col = _first_present(frame, ["related_txn_id"])
    if not id_col or not amount_col:
        raise ValueError("Bank file needs an id column and an amount column.")
    rows = []
    for _, raw in frame.iterrows():
        related = ""
        if related_col and not pd.isna(raw[related_col]):
            related = str(raw[related_col]).strip()
        rows.append(
            BankTxn(
                txn_id=str(raw[id_col]),
                posted_date=_parse_date(raw[date_col]) if date_col else None,
                description=str(raw[desc_col]) if desc_col else "",
                txn_type=str(raw[type_col]).strip().lower() if type_col else "",
                amount_cents=abs(to_cents(raw[amount_col])),
                related_txn_id=related,
            )
        )
    return rows


def _anchor_dates(order: Order) -> list[date]:
    dates = [d for d in [order.order_date, *order.shipment_dates] if d]
    return dates or []


def _date_score(txn: CardTxn, order: Order) -> int:
    if txn.posted_date is None:
        return 10_000
    anchors = _anchor_dates(order)
    if not anchors:
        return 10_000
    return min(abs((txn.posted_date - anchor).days) for anchor in anchors)


def _compatible(txn: CardTxn, order: Order, days: int = DATE_WINDOW_DAYS) -> bool:
    if txn.merchant_key != order.merchant_key:
        return False
    if txn.merchant_key == "unknown":
        return False
    anchors = _anchor_dates(order) or ([order.order_date] if order.order_date else [])
    if not anchors or txn.posted_date is None:
        return False
    return any(_within(txn.posted_date, anchor, days) for anchor in anchors)


def _result_row(
    *,
    exception_id: str,
    txn: Optional[CardTxn],
    order: Optional[Order],
    status: str,
    explanation: str,
    extra_evidence: Optional[list[str]] = None,
    bank: Optional[BankTxn] = None,
    match_group_id: str = "",
    group_txn_cents: Optional[int] = None,
    group_order_cents: Optional[int] = None,
) -> dict:
    txn_cents = txn.amount_cents if txn else 0
    order_cents = order.total_cents if order else (bank.amount_cents if bank else 0)
    if status == REFUND_NOT_FOUND and order:
        order_cents = order.refund_cents or order.total_cents
    if status == POSSIBLE_DUPLICATE:
        order_cents = 0
    if group_txn_cents is None:
        group_txn_cents = txn_cents
    if group_order_cents is None:
        group_order_cents = order_cents
    group_difference = group_txn_cents - group_order_cents
    # Split lines must not look like 54.99 matched 87.50 with a fake $0 gap.
    if status == MATCHED_SPLIT:
        line_difference = None
    else:
        line_difference = txn_cents - order_cents
    evidence = []
    if txn:
        evidence.append(txn.txn_id)
        if txn.reference:
            evidence.append(txn.reference)
    if order:
        evidence.append(order.order_id)
        if order.external_order_number:
            evidence.append(order.external_order_number)
    if bank:
        evidence.append(bank.txn_id)
    if extra_evidence:
        evidence.extend(extra_evidence)
    merchant = txn.merchant_raw if txn else (order.merchant_raw if order else "")
    if not match_group_id:
        if order:
            match_group_id = f"MG-{order.order_id}"
        elif txn:
            match_group_id = f"MG-{txn.txn_id}"
        else:
            match_group_id = exception_id
    return {
        "exception_id": exception_id,
        "transaction_id": txn.txn_id if txn else "",
        "order_id": order.order_id if order else "",
        "match_group_id": match_group_id,
        "merchant": merchant,
        "transaction_amount": cents_to_dollars(txn_cents) if txn else 0.0,
        "order_amount": cents_to_dollars(order_cents) if (order or bank) else 0.0,
        "difference": None if line_difference is None else cents_to_dollars(line_difference),
        "group_transaction_amount": cents_to_dollars(group_txn_cents),
        "group_order_amount": cents_to_dollars(group_order_cents),
        "group_difference": cents_to_dollars(group_difference),
        "reconciliation_status": status,
        "evidence_ids": ";".join(dict.fromkeys(evidence)),
        "explanation": explanation,
    }


def _is_card_payment(txn: CardTxn) -> bool:
    return txn.txn_type == "payment" or txn.merchant_key == "card_payment"


def _is_bank_card_pay(bank: BankTxn) -> bool:
    blob = merchant_key(bank.description) + bank.txn_type
    text = bank.description.lower()
    return bank.txn_type in {"transfer", "payment"} and (
        "credit" in text or "autopay" in text or "card" in text or blob.find("card") >= 0
    )


def _append_evidence(row: dict, source_id: str) -> None:
    if not source_id:
        return
    ids = [part for part in str(row.get("evidence_ids") or "").split(";") if part]
    if source_id not in ids:
        ids.append(source_id)
    row["evidence_ids"] = ";".join(ids)


def _receipt_source_id(receipt: dict) -> str:
    return str(receipt.get("source_id") or f"receipt:{receipt.get('source_filename', 'unknown')}")


def _receipt_matches_txn(txn: CardTxn, receipt: dict, orders_by_id: dict[str, Order]) -> bool:
    order_token = str(receipt.get("order_id") or "").strip()
    amount = receipt.get("total_amount")
    amount_cents = to_cents(amount) if amount is not None else None
    rdate = _parse_date(receipt.get("purchase_date"))
    rkey = merchant_key(receipt.get("merchant") or "")

    order_hit = False
    if order_token:
        if txn.related_order_id and txn.related_order_id == order_token:
            order_hit = True
        related = orders_by_id.get(txn.related_order_id)
        if related and (related.order_id == order_token or related.external_order_number == order_token):
            order_hit = True
        for order in orders_by_id.values():
            if order.order_id != order_token and order.external_order_number != order_token:
                continue
            if txn.related_order_id == order.order_id:
                order_hit = True
            elif txn.merchant_key == order.merchant_key and (
                amount_cents is None or txn.amount_cents == amount_cents or txn.amount_cents == order.total_cents
            ):
                order_hit = True
    if order_hit:
        if amount_cents is not None and txn.amount_cents != amount_cents and txn.related_order_id:
            # Split shipments: order id may match while a single charge is only part of the total.
            return True
        if amount_cents is None or txn.amount_cents == amount_cents:
            return True

    merchant_ok = rkey not in {"", "unknown"} and rkey == txn.merchant_key
    amount_ok = amount_cents is not None and amount_cents == txn.amount_cents
    date_ok = rdate is None or _within(txn.posted_date, rdate, DATE_WINDOW_DAYS)
    if merchant_ok and amount_ok and date_ok:
        return True
    return False


def _attach_confirmed_receipts(
    rows: list[dict],
    cards: list[CardTxn],
    orders_by_id: dict[str, Order],
    confirmed_receipts: Optional[list[dict]],
) -> None:
    """Attach human-confirmed receipts as evidence. Never adds receipt totals as spend."""
    confirmed = [item for item in (confirmed_receipts or []) if isinstance(item, dict) and item.get("confirmed")]
    if not confirmed:
        return
    by_id = {txn.txn_id: txn for txn in cards}
    used: set[str] = set()

    for receipt in confirmed:
        source_id = _receipt_source_id(receipt)
        if source_id in used:
            continue
        filename = receipt.get("source_filename") or source_id
        candidates = [
            txn
            for txn in cards
            if txn.txn_type == "charge" and _receipt_matches_txn(txn, receipt, orders_by_id)
        ]
        matching_rows = [row for row in rows if row.get("transaction_id") in {txn.txn_id for txn in candidates}]
        if receipt.get("order_id"):
            token = str(receipt["order_id"]).strip()
            for row in rows:
                if row.get("order_id") == token and row not in matching_rows:
                    matching_rows.append(row)
                    txn = by_id.get(row.get("transaction_id"))
                    if txn and txn not in candidates:
                        candidates.append(txn)

        if not candidates:
            if not receipt.get("merchant") and receipt.get("total_amount") is None and not receipt.get("order_id"):
                continue
            rows.append(
                _result_row(
                    exception_id=f"EX-{len(rows) + 1:03d}",
                    txn=None,
                    order=None,
                    status=NEEDS_REVIEW,
                    explanation=(
                        f"Confirmed receipt {filename} has no matching card charge. "
                        "The receipt total was not added as spend."
                    ),
                    extra_evidence=[source_id],
                    match_group_id=f"MG-{source_id}",
                )
            )
            used.add(source_id)
            continue

        groups = {row.get("match_group_id") for row in matching_rows if row.get("match_group_id")}
        unique_txn_ids = {txn.txn_id for txn in candidates}

        if len(unique_txn_ids) > 1 and len(groups) != 1:
            for row in matching_rows:
                _append_evidence(row, source_id)
                note = (
                    f" Confirmed receipt {filename} matches more than one card charge and needs review. "
                    "Receipt total was not added as extra spend."
                )
                if note.strip() not in row["explanation"]:
                    row["explanation"] += note
                if row["reconciliation_status"] == UNMATCHED:
                    row["reconciliation_status"] = NEEDS_REVIEW
            used.add(source_id)
            continue

        for row in matching_rows:
            _append_evidence(row, source_id)
            if row["reconciliation_status"] == UNMATCHED:
                txn = by_id.get(row["transaction_id"])
                row["reconciliation_status"] = MATCHED
                row["explanation"] = (
                    f"Matched posted charge to confirmed receipt {filename}. "
                    "Count spend once from the card; the receipt total is not added."
                )
                if txn and receipt.get("total_amount") is not None:
                    receipt_cents = to_cents(receipt["total_amount"])
                    row["order_amount"] = cents_to_dollars(receipt_cents)
                    row["difference"] = cents_to_dollars(txn.amount_cents - receipt_cents)
                    row["group_order_amount"] = row["order_amount"]
                    row["group_difference"] = row["difference"]
            else:
                support = f" Confirmed receipt {filename} is supporting evidence only and is not extra spend."
                if support.strip() not in row["explanation"]:
                    row["explanation"] += support
        used.add(source_id)


def reconcile(
    card_df: pd.DataFrame,
    orders_df: Optional[pd.DataFrame] = None,
    bank_df: Optional[pd.DataFrame] = None,
    evidence_files: Optional[list[str]] = None,
    confirmed_receipts: Optional[list[dict]] = None,
) -> tuple[pd.DataFrame, dict]:
    cards = normalize_cards(card_df)
    orders = normalize_orders(orders_df if orders_df is not None else pd.DataFrame())
    banks = normalize_banks(bank_df)
    evidence_files = evidence_files or []

    used_txn: set[str] = set()
    used_orders: set[str] = set()
    rows: list[dict] = []
    by_id = {txn.txn_id: txn for txn in cards}
    orders_by_id = {order.order_id: order for order in orders}

    def next_id() -> str:
        return f"EX-{len(rows) + 1:03d}"

    # Bank-funded card payments are transfers, not spend.
    for txn in cards:
        if not _is_card_payment(txn):
            continue
        bank_match = None
        for bank in banks:
            related = bank.related_txn_id == txn.txn_id
            amount_date = bank.amount_cents == txn.amount_cents and _within(
                bank.posted_date, txn.posted_date, DATE_WINDOW_DAYS
            )
            if related or (amount_date and _is_bank_card_pay(bank)):
                bank_match = bank
                break
        explanation = (
            f"Card payment {cents_to_dollars(txn.amount_cents):.2f} is a CARD_PAYMENT transfer, not spend."
        )
        if bank_match:
            explanation += f" Linked to bank {bank_match.txn_id}."
        else:
            explanation += " No matching bank transfer was found; still excluded from spend."
        rows.append(
            _result_row(
                exception_id=next_id(),
                txn=txn,
                order=None,
                status=CARD_PAYMENT,
                explanation=explanation,
                bank=bank_match,
                match_group_id=f"MG-PAY-{txn.txn_id}",
                group_txn_cents=txn.amount_cents,
                group_order_cents=bank_match.amount_cents if bank_match else txn.amount_cents,
            )
        )
        used_txn.add(txn.txn_id)

    unused_charges = [txn for txn in cards if txn.txn_id not in used_txn and txn.txn_type == "charge"]
    unused_orders = [order for order in orders if order.order_id not in used_orders]

    def consume(
        txn: CardTxn,
        order: Order,
        status: str,
        explanation: str,
        extra=None,
        group: Optional[list[CardTxn]] = None,
    ) -> None:
        members = group or [txn]
        rows.append(
            _result_row(
                exception_id=next_id(),
                txn=txn,
                order=order,
                status=status,
                explanation=explanation,
                extra_evidence=extra,
                match_group_id=f"MG-{order.order_id}",
                group_txn_cents=sum(item.amount_cents for item in members),
                group_order_cents=order.total_cents,
            )
        )
        used_txn.add(txn.txn_id)
        used_orders.add(order.order_id)

    # Prefer explicit related_order_id grouping when the column is populated.
    related_groups: dict[str, list[CardTxn]] = {}
    for txn in unused_charges:
        if txn.related_order_id and txn.related_order_id in orders_by_id:
            related_groups.setdefault(txn.related_order_id, []).append(txn)

    for order_id, group in related_groups.items():
        order = orders_by_id[order_id]
        if order.order_id in used_orders:
            continue
        group = [txn for txn in group if txn.txn_id not in used_txn]
        if not group:
            continue
        total = sum(txn.amount_cents for txn in group)
        if len(group) == 1 and group[0].amount_cents == order.total_cents:
            extra = [name for name in evidence_files if name]
            consume(
                group[0],
                order,
                MATCHED,
                f"Exact match of {cents_to_dollars(order.total_cents):.2f} using related order id.",
                extra if order.merchant_key == "amazon" else None,
            )
        elif len(group) > 1 and total == order.total_cents:
            for txn in sorted(group, key=lambda item: (item.posted_date or date.min, item.txn_id)):
                consume(
                    txn,
                    order,
                    MATCHED_SPLIT,
                    (
                        f"Split group MG-{order.order_id}: this charge is "
                        f"{cents_to_dollars(txn.amount_cents):.2f}. "
                        f"Group card total {cents_to_dollars(total):.2f} equals "
                        f"order {cents_to_dollars(order.total_cents):.2f}. "
                        "Line-level difference is blank; use group_difference."
                    ),
                    group=group,
                )
                used_orders.add(order.order_id)
        elif len(group) > 1:
            exact = [txn for txn in group if txn.amount_cents == order.total_cents]
            if exact:
                chosen = sorted(exact, key=lambda item: item.txn_id)[0]
                consume(
                    chosen,
                    order,
                    MATCHED,
                    f"Exact match of {cents_to_dollars(order.total_cents):.2f}. Additional same-amount charges need review.",
                )

    unused_charges = [txn for txn in unused_charges if txn.txn_id not in used_txn]
    unused_orders = [order for order in unused_orders if order.order_id not in used_orders]

    # Exact amount + merchant + date. Never amount-only.
    for order in sorted(unused_orders, key=lambda item: item.order_id):
        candidates = [
            txn
            for txn in unused_charges
            if txn.txn_id not in used_txn
            and txn.amount_cents == order.total_cents
            and _compatible(txn, order)
        ]
        if not candidates:
            continue
        chosen = sorted(candidates, key=lambda txn: (_date_score(txn, order), txn.txn_id))[0]
        extra = evidence_files if order.merchant_key == "amazon" else None
        consume(
            chosen,
            order,
            MATCHED,
            (
                f"Matched {cents_to_dollars(order.total_cents):.2f} on merchant '{order.merchant_key}' "
                f"within {DATE_WINDOW_DAYS} days."
            ),
            extra,
        )

    unused_charges = [txn for txn in unused_charges if txn.txn_id not in used_txn]
    unused_orders = [order for order in unused_orders if order.order_id not in used_orders]

    # Split shipments: smallest unused same-merchant subset that sums to the order total.
    for order in sorted(unused_orders, key=lambda item: item.order_id):
        pool = [
            txn
            for txn in unused_charges
            if txn.txn_id not in used_txn and _compatible(txn, order)
        ]
        winner = None
        for size in range(2, min(4, len(pool)) + 1):
            scored = []
            for combo in combinations(pool, size):
                if sum(txn.amount_cents for txn in combo) == order.total_cents:
                    score = sum(_date_score(txn, order) for txn in combo)
                    scored.append((score, combo))
            if scored:
                winner = min(scored, key=lambda item: (item[0], tuple(t.txn_id for t in item[1])))[1]
                break
        if not winner:
            continue
        group_total = sum(item.amount_cents for item in winner)
        for txn in winner:
            consume(
                txn,
                order,
                MATCHED_SPLIT,
                (
                    f"Split group MG-{order.order_id}: this charge is "
                    f"{cents_to_dollars(txn.amount_cents):.2f}. "
                    f"Group card total {cents_to_dollars(group_total):.2f} equals "
                    f"order {cents_to_dollars(order.total_cents):.2f}. "
                    "Line-level difference is blank; use group_difference."
                ),
                group=list(winner),
            )

    unused_charges = [txn for txn in unused_charges if txn.txn_id not in used_txn]
    unused_orders = [order for order in unused_orders if order.order_id not in used_orders]

    # Amount mismatch: same merchant and date window, not amount-only.
    leftover_orders = [order for order in unused_orders]
    leftover_charges = [txn for txn in unused_charges]
    for order in leftover_orders:
        candidates = [
            txn
            for txn in leftover_charges
            if txn.txn_id not in used_txn and _compatible(txn, order)
        ]
        if len(candidates) != 1:
            # If several remain, pick the closest date; if none, skip.
            if not candidates:
                continue
        chosen = sorted(candidates, key=lambda txn: (_date_score(txn, order), txn.txn_id))[0]
        gap = chosen.amount_cents - order.total_cents
        consume(
            chosen,
            order,
            AMOUNT_MISMATCH,
            (
                f"Merchant and date align, but card {cents_to_dollars(chosen.amount_cents):.2f} "
                f"differs from order {cents_to_dollars(order.total_cents):.2f} by "
                f"{cents_to_dollars(gap):.2f}. No extra fee line was invented."
            ),
        )

    # Possible duplicates of an already matched charge.
    matched_charges = [
        by_id[row["transaction_id"]]
        for row in rows
        if row["reconciliation_status"] in MATCHED_STATUSES and row["transaction_id"] in by_id
    ]
    for txn in cards:
        if txn.txn_id in used_txn or txn.txn_type != "charge":
            continue
        twin = next(
            (
                other
                for other in matched_charges
                if other.txn_id != txn.txn_id
                and other.merchant_key == txn.merchant_key
                and other.amount_cents == txn.amount_cents
                and _within(other.posted_date, txn.posted_date, DUPLICATE_WINDOW_DAYS)
            ),
            None,
        )
        if not twin:
            continue
        related_order = next(
            (orders_by_id.get(row["order_id"]) for row in rows if row["transaction_id"] == twin.txn_id),
            None,
        )
        rows.append(
            _result_row(
                exception_id=next_id(),
                txn=txn,
                order=related_order,
                status=POSSIBLE_DUPLICATE,
                explanation=(
                    f"Same merchant, date, and {cents_to_dollars(txn.amount_cents):.2f} as {twin.txn_id}. "
                    "Possible duplicate. Not counted as extra spend."
                ),
                extra_evidence=[twin.txn_id],
            )
        )
        used_txn.add(txn.txn_id)

    # Missing refund: matched (or leftover) order reports a refund with no card refund.
    refund_txns = [txn for txn in cards if txn.txn_type == "refund"]
    for order in orders:
        if order.refund_cents <= 0 and "refund" not in order.status:
            continue
        card_refund = next(
            (
                txn
                for txn in refund_txns
                if txn.amount_cents == order.refund_cents
                and txn.merchant_key == order.merchant_key
            ),
            None,
        )
        if card_refund:
            continue
        charge_row = next(
            (row for row in rows if row["order_id"] == order.order_id and row["reconciliation_status"] in MATCHED_STATUSES),
            None,
        )
        if charge_row:
            charge_row["reconciliation_status"] = REFUND_NOT_FOUND
            charge_row["order_amount"] = cents_to_dollars(order.refund_cents)
            charge_row["difference"] = cents_to_dollars(
                to_cents(charge_row["transaction_amount"]) - order.refund_cents
            )
            charge_row["explanation"] = (
                f"Order {order.order_id} shows a refund of {cents_to_dollars(order.refund_cents):.2f} "
                f"on {order.refund_date or 'an unknown date'}, but no card refund posted. "
                "Treat as review, not a confirmed merchant error. Posted charge remains spend until refunded."
            )

    # Remaining unmatched card charges.
    for txn in cards:
        if txn.txn_id in used_txn or txn.txn_type != "charge":
            continue
        rows.append(
            _result_row(
                exception_id=next_id(),
                txn=txn,
                order=None,
                status=UNMATCHED,
                explanation=(
                    f"No purchase record matched {txn.merchant_raw} "
                    f"{cents_to_dollars(txn.amount_cents):.2f}. A receipt was not invented."
                ),
            )
        )
        used_txn.add(txn.txn_id)

    for order in orders:
        if order.order_id in used_orders:
            continue
        already = any(row["order_id"] == order.order_id for row in rows)
        if already:
            continue
        rows.append(
            _result_row(
                exception_id=next_id(),
                txn=None,
                order=order,
                status=UNMATCHED,
                explanation=f"Order {order.order_id} has no matching card charge in this period.",
            )
        )

    _attach_confirmed_receipts(rows, cards, orders_by_id, confirmed_receipts)

    result = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    summary = summarize(result, card_count=len(cards))
    return result, summary


def summarize(result: pd.DataFrame, card_count: int) -> dict:
    empty = {
        "total_transactions_analyzed": card_count,
        "matched_transactions": 0,
        "exceptions_requiring_review": 0,
        "actual_posted_spending": 0.0,
        "potential_duplicate_exposure": 0.0,
        "provisional_adjusted_spending": 0.0,
    }
    if result.empty:
        return empty
    matched = result["reconciliation_status"].isin(MATCHED_STATUSES) & result["transaction_id"].ne("")
    purchase_matched = int(matched.sum())
    exceptions = int(result["reconciliation_status"].isin(EXCEPTION_STATUSES).sum())
    posted = actual_posted_spend_cents(result)
    duplicate = duplicate_exposure_cents(result)
    return {
        "total_transactions_analyzed": card_count,
        "matched_transactions": purchase_matched,
        "exceptions_requiring_review": exceptions,
        "actual_posted_spending": cents_to_dollars(posted),
        "potential_duplicate_exposure": cents_to_dollars(duplicate),
        "provisional_adjusted_spending": cents_to_dollars(posted - duplicate),
    }


def _sum_unique_charges(result: pd.DataFrame, statuses: set[str]) -> int:
    total = 0
    seen = set()
    for _, row in result.iterrows():
        txn_id = row["transaction_id"]
        status = row["reconciliation_status"]
        if not txn_id or txn_id in seen:
            continue
        if status in TRANSFER_STATUSES:
            seen.add(txn_id)
            continue
        if status in statuses:
            total += to_cents(row["transaction_amount"])
            seen.add(txn_id)
    return total


def actual_posted_spend_cents(result: pd.DataFrame) -> int:
    """Posted card charges still on the statement, including suspected duplicates."""
    return _sum_unique_charges(result, POSTED_CHARGE_STATUSES)


def duplicate_exposure_cents(result: pd.DataFrame) -> int:
    return _sum_unique_charges(result, {POSSIBLE_DUPLICATE})


def provisional_adjusted_spend_cents(result: pd.DataFrame) -> int:
    """Posted charges minus suspected duplicates. Not the statement total."""
    return actual_posted_spend_cents(result) - duplicate_exposure_cents(result)


def spend_cents(result: pd.DataFrame) -> int:
    """Actual posted card spending, including suspected duplicates."""
    return actual_posted_spend_cents(result)
