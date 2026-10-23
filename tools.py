"""Read-only investigation tools. These never write financial records."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

import pandas as pd

from reconciliation import (
    DATE_WINDOW_DAYS,
    cents_to_dollars,
    merchant_key,
    normalize_cards,
    normalize_orders,
    to_cents,
)

VERIFIED_SUPPORT = {
    "amazon": {
        "merchant_family": "amazon",
        "support_url": "https://www.amazon.com/gp/help/customer/contact-us",
        "orders_url": "https://www.amazon.com/gp/css/order-history",
        "phone": None,
        "note": "Official Amazon contact-us and order-history pages only. No phone number is stored here.",
    },
    "costco": {
        "merchant_family": "costco",
        "support_url": "https://www.costco.com/customer-service.html",
        "membership_url": "https://customerservice.costco.com/",
        "phone": None,
        "note": "Official Costco customer-service pages only. No phone number is stored here.",
    },
}

ALLOWED_TOOLS = {
    "get_transaction_details",
    "find_related_orders",
    "get_merchant_support",
}


def _parse_date(value: Any) -> Optional[date]:
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


def _error(message: str) -> dict:
    return {"ok": False, "error": message}


class InvestigationToolkit:
    """Look up posted records. Callers must not use this to edit statements."""

    def __init__(
        self,
        card_df: pd.DataFrame,
        orders_df: Optional[pd.DataFrame] = None,
        recon_df: Optional[pd.DataFrame] = None,
        email_receipts: Optional[list] = None,
    ) -> None:
        self.cards = normalize_cards(card_df if card_df is not None else pd.DataFrame())
        self.orders = normalize_orders(orders_df if orders_df is not None else pd.DataFrame())
        self.recon_by_txn = {}
        if recon_df is not None and not recon_df.empty:
            for _, row in recon_df.iterrows():
                txn_id = str(row.get("transaction_id") or "")
                if txn_id:
                    self.recon_by_txn[txn_id] = {
                        "exception_id": row.get("exception_id"),
                        "reconciliation_status": row.get("reconciliation_status"),
                        "order_id": row.get("order_id"),
                        "match_group_id": row.get("match_group_id"),
                        "difference": None
                        if pd.isna(row.get("difference"))
                        else row.get("difference"),
                    }
        self.email_receipts = [
            item.get("filename", item) if isinstance(item, dict) else str(item)
            for item in (email_receipts or [])
        ]

    def get_transaction_details(self, transaction_id: Any) -> dict:
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            return _error("transaction_id must be a non-empty string.")
        txn_id = transaction_id.strip()
        found = next((item for item in self.cards if item.txn_id == txn_id), None)
        if found is None:
            return _error(f"No card transaction {txn_id} exists in the loaded statement.")
        recon = self.recon_by_txn.get(txn_id, {})
        return {
            "ok": True,
            "transaction_id": found.txn_id,
            "posted_date": found.posted_date.isoformat() if found.posted_date else None,
            "merchant": found.merchant_raw,
            "merchant_family": found.merchant_key,
            "txn_type": found.txn_type,
            "amount": cents_to_dollars(found.amount_cents),
            "reference": found.reference,
            "related_order_id": found.related_order_id or None,
            "reconciliation_status": recon.get("reconciliation_status"),
            "exception_id": recon.get("exception_id"),
            "matched_order_id": recon.get("order_id") or None,
        }

    def find_related_orders(self, merchant: Any, amount: Any, date: Any) -> dict:
        if not isinstance(merchant, str) or not merchant.strip():
            return _error("merchant must be a non-empty string.")
        try:
            amount_cents = to_cents(amount)
        except Exception:
            return _error("amount must be a numeric dollar value.")
        posted = _parse_date(date)
        if posted is None:
            return _error("date must be a real calendar date such as 2026-09-20.")
        family = merchant_key(merchant)
        window = timedelta(days=DATE_WINDOW_DAYS)
        related = []
        for order in self.orders:
            if family in {"unknown", "card_payment"} or order.merchant_key != family:
                continue
            anchors = [order.order_date, *order.shipment_dates]
            date_hit = any(anchor and abs(anchor - posted) <= window for anchor in anchors if anchor)
            amount_hit = abs(order.total_cents - amount_cents) <= 500 or (
                order.refund_cents and abs(order.refund_cents - amount_cents) <= 1
            )
            if not (date_hit or amount_hit):
                continue
            if not date_hit and order.merchant_key != family:
                continue
            related.append(
                {
                    "order_id": order.order_id,
                    "external_order_number": order.external_order_number or None,
                    "merchant": order.merchant_raw,
                    "order_date": order.order_date.isoformat() if order.order_date else None,
                    "order_total": cents_to_dollars(order.total_cents),
                    "order_status": order.status,
                    "refund_amount": cents_to_dollars(order.refund_cents),
                    "refund_date": order.refund_date.isoformat() if order.refund_date else None,
                    "date_window_hit": date_hit,
                    "amount_window_hit": amount_hit,
                }
            )
        related = related[:10]
        receipts = []
        if family == "amazon":
            receipts = list(self.email_receipts)
        return {
            "ok": True,
            "merchant_family": family,
            "query_amount": cents_to_dollars(amount_cents),
            "query_date": posted.isoformat(),
            "orders": related,
            "email_receipt_filenames": receipts,
        }

    def get_merchant_support(self, merchant: Any) -> dict:
        if not isinstance(merchant, str) or not merchant.strip():
            return _error("merchant must be a non-empty string.")
        family = merchant_key(merchant)
        verified = VERIFIED_SUPPORT.get(family)
        if not verified:
            return {
                "ok": True,
                "merchant_family": family,
                "support_url": None,
                "phone": None,
                "note": (
                    f"No verified official support URL is stored for '{merchant}'. "
                    "Phone numbers and extra contact details were not invented."
                ),
            }
        payload = {"ok": True, **verified}
        return payload

    def execute(self, name: str, arguments: dict) -> dict:
        if name not in ALLOWED_TOOLS:
            return _error(f"Unknown tool {name}.")
        if not isinstance(arguments, dict):
            return _error("Tool arguments must be an object.")
        if name == "get_transaction_details":
            return self.get_transaction_details(arguments.get("transaction_id"))
        if name == "find_related_orders":
            return self.find_related_orders(
                arguments.get("merchant"),
                arguments.get("amount"),
                arguments.get("date"),
            )
        return self.get_merchant_support(arguments.get("merchant"))


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": "get_transaction_details",
        "description": "Look up one posted card transaction by txn_id. Read-only.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "transaction_id": {
                    "type": "string",
                    "description": "Card txn_id such as CC-202609-006",
                }
            },
            "required": ["transaction_id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "find_related_orders",
        "description": (
            "Search loaded Amazon/Costco orders and email receipt filenames. "
            "Uses merchant family (Amazon vs Costco) plus date/amount windows. Not amount-only across merchants."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "merchant": {"type": "string"},
                "amount": {"type": "number"},
                "date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
            },
            "required": ["merchant", "amount", "date"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_merchant_support",
        "description": (
            "Return a verified official Amazon or Costco support URL. "
            "Returns no URL for other merchants. Never returns phone numbers."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"merchant": {"type": "string"}},
            "required": ["merchant"],
            "additionalProperties": False,
        },
    },
]
