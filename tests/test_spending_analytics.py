import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest
from openai import APIError

from loaders import load_sample_bundle
from reconciliation import CARD_PAYMENT, actual_posted_spend_cents, reconcile, to_cents
from spending_analytics import (
    UNCATEGORIZED,
    analyze_spending,
    categorize_item,
    insight_payload,
)
from spending_insights import SpendingInsightsError, generate_spending_insights

ROOT = Path(__file__).resolve().parents[1]
GT = json.loads((ROOT / "sample_data" / "ground_truth.json").read_text())


def _sample_report(**kwargs):
    bundle = load_sample_bundle()
    result, _ = reconcile(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        bundle["bank"]["df"],
        evidence_files=[item["filename"] for item in bundle["email_receipts"]],
    )
    report = analyze_spending(
        result,
        card_df=bundle["credit_card"]["df"],
        orders_df=bundle["online_orders"]["df"],
        bank_df=bundle["bank"]["df"],
        confirmed_receipts=kwargs.get("confirmed_receipts") or [],
    )
    return bundle, result, report


def test_total_posted_spending_matches_ground_truth():
    _, result, report = _sample_report()
    posted = to_cents(GT["totals"]["card_charge_sum"])
    assert report["posted_spending_cents"] == posted
    assert report["posted_spending_cents"] == actual_posted_spend_cents(result)
    assert report["net_spending_cents"] == posted
    assert report["posted_refunds_cents"] == 0


def test_category_totals_and_percentages():
    _, _, report = _sample_report()
    cat_sum = sum(item["amount_cents"] for item in report["categories"])
    assert cat_sum == report["posted_spending_cents"]
    percent_sum = sum(item["percent"] for item in report["categories"])
    assert abs(percent_sum - 100) <= 0.1
    by_name = {item["category"]: item for item in report["categories"]}
    assert "shopping" in by_name
    assert "groceries" in by_name
    assert "household" in by_name
    assert by_name["household"]["amount_cents"] > 0
    assert by_name["groceries"]["amount_cents"] > 0
    costco_row = next(row for row in report["transactions"] if row["transaction_id"] == "CC-202609-008")
    assert "household" in costco_row["spending_category"]
    assert "groceries" in costco_row["spending_category"]
    assert costco_row["spending_category"] != "groceries"


def test_costco_is_not_forced_to_groceries():
    assert categorize_item("Kirkland paper towels 12 pack") == "household"
    assert categorize_item("Kirkland olive oil 2-pack") == "groceries"
    assert categorize_item("Kirkland AAA batteries 48 count") == "household"


def test_merchant_rankings():
    _, _, report = _sample_report()
    assert len(report["top_merchants"]) <= 5
    top = report["top_merchants"][0]
    assert top["merchant_key"] == "amazon"
    assert top["transaction_count"] >= 1
    assert to_cents(top["amount"]) == top["amount_cents"]
    keys = [item["merchant_key"] for item in report["top_merchants"]]
    assert "costco" in keys
    assert "wholefoods" in keys
    assert "card_payment" not in keys


def test_posted_refunds_reduce_net_only():
    bundle = load_sample_bundle()
    cards = bundle["credit_card"]["df"].copy()
    refund = cards.iloc[[0]].copy()
    refund["txn_id"] = "CC-202609-R01"
    refund["txn_type"] = "refund"
    refund["amount"] = 10.00
    refund["related_order_id"] = ""
    refund["merchant_raw"] = "AMAZON.COM*REFUND"
    cards = pd.concat([cards, refund], ignore_index=True)
    result, _ = reconcile(cards, bundle["online_orders"]["df"], bundle["bank"]["df"])
    report = analyze_spending(
        result,
        card_df=cards,
        orders_df=bundle["online_orders"]["df"],
        bank_df=bundle["bank"]["df"],
    )
    assert report["posted_refunds_cents"] == to_cents(10)
    assert report["posted_spending_cents"] == to_cents(GT["totals"]["card_charge_sum"])
    assert report["net_spending_cents"] == report["posted_spending_cents"] - to_cents(10)
    assert report["net_spending_cents"] != report["posted_spending_cents"]


def test_unverified_order_refund_is_not_subtracted():
    _, result, report = _sample_report()
    assert "REFUND_NOT_FOUND" in set(result["reconciliation_status"])
    assert report["posted_refunds_cents"] == 0
    assert report["net_spending_cents"] == report["posted_spending_cents"]
    assert any("pending refund" in warning.lower() or "not subtracted" in warning.lower() for warning in report["warnings"])


def test_transfers_and_card_payments_excluded():
    _, result, report = _sample_report()
    ids = {row["transaction_id"] for row in report["transactions"]}
    assert "CC-202609-010" not in ids
    assert "BK-202609-002" not in ids
    assert "BK-202609-003" not in ids
    assert CARD_PAYMENT in set(result["reconciliation_status"])
    assert report["posted_spending_cents"] != to_cents(1204)
    assert all(row["reconciliation_status"] != CARD_PAYMENT for row in report["transactions"])


def test_suspected_duplicates_stay_in_posted_totals():
    _, _, report = _sample_report()
    dup = next(row for row in report["transactions"] if row["transaction_id"] == "CC-202609-005")
    assert dup["reconciliation_status"] == "POSSIBLE_DUPLICATE"
    assert report["duplicate_exposure_cents"] == to_cents(29.99)
    assert report["posted_spending_cents"] == to_cents(437.35)
    assert report["posted_spending_cents"] != to_cents(437.35) - to_cents(29.99)


def test_no_double_counting_orders_or_receipts():
    _, result, report = _sample_report()
    orders = to_cents(GT["totals"]["order_total_if_summed_by_distinct_order_id"])
    posted = report["posted_spending_cents"]
    assert posted != posted + orders
    assert posted == to_cents(GT["totals"]["card_charge_sum"])
    txn_ids = [row["transaction_id"] for row in report["transactions"]]
    assert txn_ids == list(dict.fromkeys(txn_ids))
    assert actual_posted_spend_cents(result) == posted


def test_unknown_categories_preserved():
    bundle = load_sample_bundle()
    extra = pd.DataFrame(
        [
            {
                "txn_id": "CC-MYSTERY-1",
                "posted_date": "2026-09-23",
                "merchant_raw": "ZZZ UNKNOWN VENDOR",
                "txn_type": "charge",
                "amount": 11.11,
                "related_order_id": "",
            }
        ]
    )
    cards = pd.concat([bundle["credit_card"]["df"], extra], ignore_index=True)
    result, _ = reconcile(cards, bundle["online_orders"]["df"], bundle["bank"]["df"])
    report = analyze_spending(
        result,
        card_df=cards,
        orders_df=bundle["online_orders"]["df"],
        bank_df=bundle["bank"]["df"],
    )
    mystery = next(row for row in report["transactions"] if row["transaction_id"] == "CC-MYSTERY-1")
    assert mystery["spending_category"] == UNCATEGORIZED
    uncategorized = next(item for item in report["categories"] if item["category"] == UNCATEGORIZED)
    assert uncategorized["amount_cents"] >= to_cents(11.11)


def test_missing_receipt_details_still_count_unmatched_spend():
    _, _, report = _sample_report()
    grocery = next(row for row in report["transactions"] if row["transaction_id"] == "CC-202609-009")
    assert grocery["reconciliation_status"] == "UNMATCHED"
    assert grocery["amount_cents"] == to_cents(72.40)
    assert report["unmatched_transactions"] >= 1
    assert grocery["spending_category"] == "groceries"


def test_bank_expense_included_without_double_counting_card():
    bundle = load_sample_bundle()
    banks = bundle["bank"]["df"].copy()
    extra = banks.iloc[[0]].copy()
    extra["txn_id"] = "BK-202609-099"
    extra["txn_type"] = "purchase"
    extra["amount"] = 18.50
    extra["description"] = "LOCAL CAFE DOWNTOWN"
    extra["related_txn_id"] = ""
    banks = pd.concat([banks, extra], ignore_index=True)
    result, _ = reconcile(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        banks,
    )
    report = analyze_spending(
        result,
        card_df=bundle["credit_card"]["df"],
        orders_df=bundle["online_orders"]["df"],
        bank_df=banks,
    )
    assert any(row["transaction_id"] == "BK-202609-099" for row in report["transactions"])
    assert report["posted_spending_cents"] == to_cents(437.35) + to_cents(18.50)
    assert "BK-202609-002" not in {row["transaction_id"] for row in report["transactions"]}


def test_quality_checks_pass_on_sample():
    _, _, report = _sample_report()
    failed = [item for item in report["checks"] if not item["ok"]]
    assert failed == []


def test_insight_payload_is_aggregates_only():
    _, _, report = _sample_report()
    payload = insight_payload(report)
    dumped = json.dumps(payload)
    assert "CC-202609-001" in dumped or payload["exceptions_requiring_review"] >= 0
    assert "posted_spending" in payload
    assert "categories" in payload
    assert "limitations" in payload
    assert "month-over-month" in " ".join(payload["limitations"]).lower()


class ScriptedClient:
    def __init__(self, text=None, error=None):
        self.text = text
        self.error = error
        self.calls = []
        self.responses = SimpleNamespace()
        self.responses.create = self.create

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(output_text=self.text, output=[])


def test_ai_insights_use_mocked_aggregates():
    _, _, report = _sample_report()
    body = {
        "period": "2026-09",
        "largest_categories_summary": "Fact: shopping is the largest category in the provided totals.",
        "merchant_concentration": "Fact: Amazon is the top merchant in the provided ranks.",
        "recommendations": ["Review the unmatched Whole Foods charge using a confirmed receipt."],
        "unresolved_risks": ["Possible duplicate 29.99 remains posted."],
        "further_review": ["Investigate the 5.00 amount mismatch."],
        "limitations": ["Only September 2026 is available."],
        "fact_versus_suggestion_note": "Facts cite payload numbers; suggestions are review actions.",
    }
    client = ScriptedClient(json.dumps(body))
    result = generate_spending_insights(report, client=client)
    assert result["period"] == "2026-09"
    assert "shopping" in result["largest_categories_summary"].lower() or "Fact" in result["largest_categories_summary"]
    assert client.calls
    prompt = client.calls[0]["input"][0]["content"]
    assert "BEGIN VERIFIED AGGREGATES" in prompt
    assert "Do not claim month-over-month" in prompt
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    failing = ScriptedClient(error=APIError("synthetic", request, body=None))
    with pytest.raises(SpendingInsightsError):
        generate_spending_insights(report, client=failing)
