from pathlib import Path
import json

import pandas as pd

from loaders import load_sample_bundle
from reconciliation import (
    AMOUNT_MISMATCH,
    CARD_PAYMENT,
    MATCHED,
    MATCHED_SPLIT,
    POSSIBLE_DUPLICATE,
    REFUND_NOT_FOUND,
    UNMATCHED,
    actual_posted_spend_cents,
    cents_to_dollars,
    duplicate_exposure_cents,
    provisional_adjusted_spend_cents,
    reconcile,
    to_cents,
)

ROOT = Path(__file__).resolve().parents[1]
GT = json.loads((ROOT / "sample_data" / "ground_truth.json").read_text())


def _run():
    bundle = load_sample_bundle()
    result, summary = reconcile(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        bundle["bank"]["df"],
        evidence_files=[item["filename"] for item in bundle["email_receipts"]],
    )
    by_txn = {
        row.transaction_id: row
        for row in result.itertuples(index=False)
        if row.transaction_id
    }
    return result, summary, by_txn


def test_normal_matching():
    _, _, by_txn = _run()
    row = by_txn["CC-202609-001"]
    assert row.reconciliation_status == MATCHED
    assert row.order_id == "ORD-AMZ-1001"
    assert to_cents(row.transaction_amount) == to_cents(42.16)
    assert to_cents(row.order_amount) == to_cents(42.16)
    assert to_cents(row.difference) == 0


def test_merchant_name_variation():
    _, _, by_txn = _run()
    row = by_txn["CC-202609-008"]
    assert row.reconciliation_status == MATCHED
    assert row.order_id == "ORD-COSTCO-2001"
    assert to_cents(row.transaction_amount) == to_cents(95.33)


def test_split_charges():
    result, _, by_txn = _run()
    first = by_txn["CC-202609-002"]
    second = by_txn["CC-202609-003"]
    assert first.reconciliation_status == MATCHED_SPLIT
    assert second.reconciliation_status == MATCHED_SPLIT
    assert first.order_id == second.order_id == "ORD-AMZ-1002"
    assert first.match_group_id == second.match_group_id == "MG-ORD-AMZ-1002"
    split_sum = to_cents(first.transaction_amount) + to_cents(second.transaction_amount)
    assert split_sum == to_cents(87.50)
    assert split_sum == to_cents(
        result.loc[result["order_id"] == "ORD-AMZ-1002", "transaction_amount"].sum()
    )
    assert to_cents(first.group_transaction_amount) == to_cents(87.50)
    assert to_cents(first.group_order_amount) == to_cents(87.50)
    assert to_cents(first.group_difference) == 0
    assert to_cents(second.group_difference) == 0
    assert pd.isna(first.difference)
    assert pd.isna(second.difference)


def test_duplicate_charge_detection():
    _, _, by_txn = _run()
    assert by_txn["CC-202609-004"].reconciliation_status == MATCHED
    assert by_txn["CC-202609-004"].order_id == "ORD-AMZ-1003"
    dup = by_txn["CC-202609-005"]
    assert dup.reconciliation_status == POSSIBLE_DUPLICATE
    assert to_cents(dup.transaction_amount) == to_cents(29.99)


def test_missing_refund():
    _, _, by_txn = _run()
    row = by_txn["CC-202609-007"]
    assert row.reconciliation_status == REFUND_NOT_FOUND
    assert row.order_id == "ORD-AMZ-1005"
    assert to_cents(row.transaction_amount) == to_cents(24.99)


def test_amount_mismatch():
    _, _, by_txn = _run()
    row = by_txn["CC-202609-006"]
    assert row.reconciliation_status == AMOUNT_MISMATCH
    assert row.order_id == "ORD-AMZ-1004"
    assert to_cents(row.transaction_amount) == to_cents(54.99)
    assert to_cents(row.order_amount) == to_cents(49.99)
    assert to_cents(row.difference) == to_cents(5.00)


def test_unmatched_purchase_without_receipt():
    _, _, by_txn = _run()
    row = by_txn["CC-202609-009"]
    assert row.reconciliation_status == UNMATCHED
    assert row.order_id == ""
    assert to_cents(row.transaction_amount) == to_cents(72.40)


def test_credit_card_payment_excluded_from_spend():
    result, summary, by_txn = _run()
    row = by_txn["CC-202609-010"]
    assert row.reconciliation_status == CARD_PAYMENT
    assert "BK-202609-002" in row.evidence_ids
    assert to_cents(row.transaction_amount) == to_cents(1204)
    posted = actual_posted_spend_cents(result)
    assert posted != to_cents(1204)
    assert to_cents(summary["actual_posted_spending"]) == posted


def test_no_double_counting():
    result, summary, _ = _run()
    card_charges = to_cents(GT["totals"]["card_charge_sum"])
    duplicate = to_cents(GT["totals"]["duplicate_excluded_from_spend"])
    orders = to_cents(GT["totals"]["order_total_if_summed_by_distinct_order_id"])
    posted = actual_posted_spend_cents(result)
    # Orders are evidence: spend must not be charges plus order totals.
    assert posted != card_charges + orders
    assert posted == card_charges
    assert posted != posted + to_cents(1204)
    assert to_cents(summary["actual_posted_spending"]) == posted
    assert to_cents(summary["potential_duplicate_exposure"]) == duplicate
    assert to_cents(summary["provisional_adjusted_spending"]) == card_charges - duplicate
    assert to_cents(summary["provisional_adjusted_spending"]) == to_cents(
        GT["totals"]["posted_spend_counted_once"]
    )


def test_posted_spend_keeps_suspected_duplicate():
    result, summary, by_txn = _run()
    assert by_txn["CC-202609-005"].reconciliation_status == POSSIBLE_DUPLICATE
    posted = actual_posted_spend_cents(result)
    exposure = duplicate_exposure_cents(result)
    provisional = provisional_adjusted_spend_cents(result)
    assert posted == to_cents(GT["totals"]["card_charge_sum"])
    assert exposure == to_cents(29.99)
    assert provisional == posted - exposure
    assert posted != provisional
    assert to_cents(summary["actual_posted_spending"]) == posted
    assert cents_to_dollars(posted) == 437.35


def test_card_payment_status_is_not_matched():
    _, summary, by_txn = _run()
    row = by_txn["CC-202609-010"]
    assert row.reconciliation_status == CARD_PAYMENT
    assert row.reconciliation_status != MATCHED
    assert to_cents(row.group_difference) == 0
    assert summary["matched_transactions"] == 5


def test_each_card_txn_appears_once():
    result, _, _ = _run()
    txn_ids = result.loc[result["transaction_id"] != "", "transaction_id"]
    assert list(txn_ids) == list(dict.fromkeys(txn_ids))
    expected = {item["txn_id"] for item in GT["posted_card_charges"]}
    expected.add("CC-202609-010")
    assert expected == set(txn_ids)


def test_does_not_match_on_amount_alone():
    """A Whole Foods 72.40 charge must not attach to an unrelated 72.40-less order."""
    _, _, by_txn = _run()
    assert by_txn["CC-202609-009"].order_id == ""
    assert by_txn["CC-202609-002"].order_id == "ORD-AMZ-1002"
    assert by_txn["CC-202609-006"].order_id == "ORD-AMZ-1004"
