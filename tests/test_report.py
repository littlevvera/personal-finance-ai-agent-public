import csv
import io
from html import unescape

import pandas as pd

from loaders import load_sample_bundle
from reconciliation import EXCEPTION_STATUSES, actual_posted_spend_cents, reconcile, to_cents
from report import DISCLOSURE, NOT_INVESTIGATED, exceptions_csv, generate_monthly_report


def _sample_recon():
    bundle = load_sample_bundle()
    result, summary = reconcile(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        bundle["bank"]["df"],
        evidence_files=[item["filename"] for item in bundle["email_receipts"]],
    )
    return bundle, result, summary


def test_report_generation_includes_required_sections():
    bundle, result, summary = _sample_recon()
    payload = generate_monthly_report(
        result,
        card_df=bundle["credit_card"]["df"],
        orders_df=bundle["online_orders"]["df"],
        bank_df=bundle["bank"]["df"],
        recon_summary=summary,
    )
    html = payload["html"]
    assert payload["complete"] is True
    assert "<!DOCTYPE html>" in html
    for heading in (
        "Executive summary",
        "Spending analysis",
        "Reconciliation results",
        "AI investigation findings",
        "Recommended next actions",
        "AI spending insights",
    ):
        assert heading in html
    assert DISCLOSURE.split(".")[0] in unescape(html)
    assert "2026-09" in html


def test_report_financial_totals_match_analytics():
    import json
    from pathlib import Path

    gt = json.loads((Path(__file__).resolve().parents[1] / "sample_data" / "ground_truth.json").read_text())
    bundle, result, summary = _sample_recon()
    payload = generate_monthly_report(
        result,
        card_df=bundle["credit_card"]["df"],
        orders_df=bundle["online_orders"]["df"],
        bank_df=bundle["bank"]["df"],
        recon_summary=summary,
    )
    html = payload["html"]
    posted = to_cents(gt["totals"]["card_charge_sum"])
    assert actual_posted_spend_cents(result) == posted
    assert "$437.35" in html
    assert "CARD_PAYMENT" not in html or "excluded" in html.lower()
    assert "CC-202609-010" not in html or "excluded" in html.lower()
    spend = payload["spend"]
    assert spend["posted_spending_cents"] == posted
    assert spend["net_spending_cents"] == posted
    assert spend["duplicate_exposure_cents"] == to_cents(29.99)
    cat_sum = sum(item["amount_cents"] for item in spend["categories"])
    assert cat_sum == posted


def test_exceptions_are_included():
    bundle, result, summary = _sample_recon()
    payload = generate_monthly_report(
        result,
        card_df=bundle["credit_card"]["df"],
        recon_summary=summary,
    )
    html = payload["html"]
    exception_rows = result[result["reconciliation_status"].isin(EXCEPTION_STATUSES)]
    assert not exception_rows.empty
    for eid in exception_rows["exception_id"]:
        assert str(eid) in html
    assert "UNMATCHED" in html
    assert "POSSIBLE_DUPLICATE" in html
    assert "AMOUNT_MISMATCH" in html
    assert "REFUND_NOT_FOUND" in html


def test_missing_ai_investigation_is_labeled():
    bundle, result, summary = _sample_recon()
    payload = generate_monthly_report(
        result,
        card_df=bundle["credit_card"]["df"],
        recon_summary=summary,
        investigations={},
    )
    assert NOT_INVESTIGATED in payload["html"]
    investigated_id = result.loc[result["reconciliation_status"].isin(EXCEPTION_STATUSES), "exception_id"].iloc[0]
    payload_one = generate_monthly_report(
        result,
        card_df=bundle["credit_card"]["df"],
        recon_summary=summary,
        investigations={
            investigated_id: {
                "result": {
                    "investigation_summary": "Fact: card CC-202609-006 posted 54.99 versus order 49.99.",
                    "possible_root_cause": "Hypothesis: fee not in the order file.",
                    "supporting_evidence_ids": ["CC-202609-006", "ORD-AMZ-1004"],
                    "review_status": "needs_review",
                    "recommended_action": "Keep open for review.",
                    "merchant_support_url": "https://www.amazon.com/gp/help/customer/display.html",
                    "customer_service_message": "Please review posted charge CC-202609-006.",
                }
            }
        },
    )
    assert "Fact: card CC-202609-006 posted 54.99 versus order 49.99." in payload_one["html"]
    assert payload_one["html"].count(NOT_INVESTIGATED) >= 1


def test_html_escapes_untrusted_content():
    cards = pd.DataFrame(
        [
            {
                "txn_id": "CC-X",
                "posted_date": "2026-09-01",
                "merchant_raw": "<script>alert(1)</script>",
                "txn_type": "charge",
                "amount": 10.00,
            }
        ]
    )
    result, summary = reconcile(cards)
    payload = generate_monthly_report(result, card_df=cards, recon_summary=summary)
    html = payload["html"]
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_csv_export_includes_exceptions_and_investigation_status():
    bundle, result, summary = _sample_recon()
    csv_text = exceptions_csv(result, investigations={})
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    assert rows
    assert "investigation_status" in reader.fieldnames
    assert all(row["investigation_status"] == NOT_INVESTIGATED for row in rows)
    statuses = {row["reconciliation_status"] for row in rows}
    assert "UNMATCHED" in statuses
    payload = generate_monthly_report(result, recon_summary=summary)
    assert payload["csv"] == csv_text


def test_html_layout_is_self_contained_and_printable():
    bundle, result, summary = _sample_recon()
    html = generate_monthly_report(
        result,
        card_df=bundle["credit_card"]["df"],
        orders_df=bundle["online_orders"]["df"],
        bank_df=bundle["bank"]["df"],
        recon_summary=summary,
    )["html"]
    assert "<svg" in html
    assert "cdn." not in html.lower()
    assert "streamlit" not in html.lower()
    assert "@page" in html
    assert "letter" in html
    assert "@media print" in html
    assert "break-inside: avoid-page" in html
    assert "class='wrap'" in html or 'class="wrap"' in html
    assert "max-width: 700px" in html
    assert html.count("<svg") >= 1


def test_missing_or_incomplete_input():
    payload = generate_monthly_report(None)
    assert payload["complete"] is False
    assert "No reconciliation result is available" in payload["error"]
    assert "not invented" in payload["html"].lower() or "Missing values were not invented" in payload["html"]
    empty = generate_monthly_report(pd.DataFrame())
    assert empty["complete"] is False
    csv_text = exceptions_csv(pd.DataFrame())
    assert csv_text.startswith("exception_id")
