from loaders import load_sample_bundle
from reconciliation import reconcile
from tools import InvestigationToolkit, VERIFIED_SUPPORT


def _toolkit():
    bundle = load_sample_bundle()
    result, _ = reconcile(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        bundle["bank"]["df"],
    )
    return InvestigationToolkit(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        recon_df=result,
        email_receipts=bundle["email_receipts"],
    ), result


def test_get_transaction_details_amount_mismatch():
    tools, _ = _toolkit()
    payload = tools.get_transaction_details("CC-202609-006")
    assert payload["ok"] is True
    assert payload["amount"] == 54.99
    assert payload["reconciliation_status"] == "AMOUNT_MISMATCH"
    assert payload["matched_order_id"] == "ORD-AMZ-1004"


def test_get_transaction_details_rejects_blank():
    tools, _ = _toolkit()
    assert tools.get_transaction_details("")["ok"] is False
    assert tools.get_transaction_details("NO-SUCH")["ok"] is False


def test_find_related_orders_supports_amazon_name_variation():
    tools, _ = _toolkit()
    payload = tools.find_related_orders("AMZN MKTP US*QW9", 54.99, "2026-09-20")
    assert payload["ok"] is True
    ids = {item["order_id"] for item in payload["orders"]}
    assert "ORD-AMZ-1004" in ids
    assert payload["merchant_family"] == "amazon"


def test_find_related_orders_missing_refund_and_duplicate():
    tools, _ = _toolkit()
    refund = tools.find_related_orders("AMAZON.COM*CASE4H", 24.99, "2026-09-05")
    refund_ids = {item["order_id"] for item in refund["orders"]}
    assert "ORD-AMZ-1005" in refund_ids
    refund_row = next(item for item in refund["orders"] if item["order_id"] == "ORD-AMZ-1005")
    assert refund_row["refund_amount"] == 24.99
    dup = tools.find_related_orders("AMAZON.COM*9X2Q", 29.99, "2026-09-18")
    assert "ORD-AMZ-1003" in {item["order_id"] for item in dup["orders"]}


def test_find_related_orders_unmatched_has_no_invented_receipt():
    tools, _ = _toolkit()
    payload = tools.find_related_orders("WHOLEFDS #10555", 72.40, "2026-09-22")
    assert payload["ok"] is True
    assert payload["orders"] == []


def test_get_merchant_support_verified_only():
    tools, _ = _toolkit()
    amazon = tools.get_merchant_support("AMZN MKTP US*QW9")
    assert amazon["support_url"] == VERIFIED_SUPPORT["amazon"]["support_url"]
    assert amazon["phone"] is None
    costco = tools.get_merchant_support("COSTCO.COM *8282")
    assert "costco.com" in costco["support_url"]
    unknown = tools.get_merchant_support("WHOLEFDS #10555")
    assert unknown["support_url"] is None
    assert unknown["phone"] is None
    assert "not invented" in unknown["note"].lower()
