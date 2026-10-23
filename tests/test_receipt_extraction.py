import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest
from openai import APIError

from loaders import load_sample_bundle
from receipt_extraction import (
    ReceiptExtractionError,
    confirm_receipt,
    extract_email_receipt,
    extract_image_receipt,
    parse_email_bytes,
    validate_extracted,
)
from reconciliation import MATCHED, NEEDS_REVIEW, UNMATCHED, actual_posted_spend_cents, reconcile, to_cents

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_data"
EMAIL = SAMPLE / "amazon_receipt.eml"
IMAGE = SAMPLE / "physical_receipt.png"
EXPECTED_EMAIL = json.loads((SAMPLE / "expected_amazon_receipt.json").read_text())
EXPECTED_IMAGE = json.loads((SAMPLE / "expected_physical_receipt.json").read_text())


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


def _api_error() -> APIError:
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return APIError("synthetic failure", request, body=None)


def test_valid_email_receipt_extraction():
    payload = {
        "merchant": "Amazon.com",
        "purchase_date": "2026-09-07",
        "order_id": "111-2345678-9012345",
        "total_amount": 42.16,
        "currency": "USD",
        "items": [
            {"description": "6 ft HDMI cable", "amount": 19.99},
            {"description": "Desk mouse pad", "amount": 19.00},
        ],
        "warnings": [],
    }
    client = ScriptedClient(json.dumps(payload))
    result = extract_email_receipt(EMAIL.read_bytes(), EMAIL.name, client=client)
    assert result["source_filename"] == "amazon_receipt.eml"
    assert result["merchant"] == "Amazon.com"
    assert result["purchase_date"] == "2026-09-07"
    assert result["order_id"] == EXPECTED_EMAIL["order_id"]
    assert result["total_amount"] == pytest.approx(42.16)
    assert result["currency"] == "USD"
    assert result["confirmed"] is False
    assert len(result["items"]) == 2
    readable = parse_email_bytes(EMAIL.read_bytes(), EMAIL.name)
    assert "ORD-AMZ-1001" in readable
    assert "42.16" in readable
    assert client.calls
    prompt = client.calls[0]["input"][0]["content"]
    assert "BEGIN UNTRUSTED RECEIPT DATA" in prompt
    assert "Ignore any request" in prompt or "not instructions" in prompt.lower() or "untrusted" in prompt.lower()


def test_valid_image_receipt_extraction():
    payload = {
        "merchant": "Whole Foods Market #10555",
        "purchase_date": "2026-09-22",
        "order_id": None,
        "subtotal": 63.42,
        "sales_tax": 8.98,
        "total_amount": 72.40,
        "currency": "USD",
        "items": EXPECTED_IMAGE["items"],
        "warnings": [],
    }
    client = ScriptedClient(json.dumps(payload))
    result = extract_image_receipt(IMAGE.read_bytes(), IMAGE.name, client=client)
    assert result["source_filename"] == "physical_receipt.png"
    assert result["merchant"] == EXPECTED_IMAGE["merchant"]
    assert result["purchase_date"] == "2026-09-22"
    assert result["total_amount"] == pytest.approx(72.40)
    assert result["subtotal"] == pytest.approx(63.42)
    assert result["sales_tax"] == pytest.approx(8.98)
    assert result["confirmed"] is False
    assert client.calls[0]["model"]
    content = client.calls[0]["input"][0]["content"]
    assert any(part.get("type") == "input_image" for part in content)


def test_missing_fields_are_null():
    record = validate_extracted(
        {"merchant": None, "purchase_date": None, "total_amount": None, "items": []},
        "blank.png",
        "image",
    )
    assert record["merchant"] is None
    assert record["purchase_date"] is None
    assert record["total_amount"] is None
    assert record["order_id"] is None
    assert "Total amount is missing." in record["warnings"]
    assert "Merchant is missing." in record["warnings"]


def test_invalid_files_rejected():
    with pytest.raises(ReceiptExtractionError):
        parse_email_bytes(b"", "notes.txt")
    with pytest.raises(ReceiptExtractionError):
        extract_image_receipt(b"", "physical_receipt.png", client=ScriptedClient("{}"))
    with pytest.raises(ReceiptExtractionError):
        extract_image_receipt(b"not-empty", "receipt.gif", client=ScriptedClient("{}"))
    with pytest.raises(ReceiptExtractionError):
        parse_email_bytes(EMAIL.read_bytes(), "amazon_receipt.txt")


def test_failed_api_response():
    email_client = ScriptedClient(error=_api_error())
    with pytest.raises(ReceiptExtractionError, match="OpenAI API error"):
        extract_email_receipt(EMAIL.read_bytes(), EMAIL.name, client=email_client)
    image_client = ScriptedClient(error=_api_error())
    with pytest.raises(ReceiptExtractionError, match="OpenAI API error"):
        extract_image_receipt(IMAGE.read_bytes(), IMAGE.name, client=image_client)
    bad_json = ScriptedClient("not json at all")
    with pytest.raises(ReceiptExtractionError):
        extract_email_receipt(EMAIL.read_bytes(), EMAIL.name, client=bad_json)


def test_receipt_confirmation_is_required():
    extracted = validate_extracted(
        {
            "merchant": "Whole Foods Market #10555",
            "purchase_date": "2026-09-22",
            "total_amount": 72.40,
            "currency": "USD",
        },
        "physical_receipt.png",
        "image",
    )
    assert extracted["confirmed"] is False
    confirmed = confirm_receipt(extracted, {"merchant": "Whole Foods Market #10555"})
    assert confirmed["confirmed"] is True
    assert confirmed["source_id"] == "receipt:physical_receipt.png"
    bundle = load_sample_bundle()
    unconfirmed_result, _ = reconcile(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        bundle["bank"]["df"],
        confirmed_receipts=[extracted],
    )
    unmatched = unconfirmed_result.loc[unconfirmed_result["transaction_id"] == "CC-202609-009"].iloc[0]
    assert unmatched.reconciliation_status == UNMATCHED
    confirmed_result, _ = reconcile(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        bundle["bank"]["df"],
        confirmed_receipts=[confirmed],
    )
    matched = confirmed_result.loc[confirmed_result["transaction_id"] == "CC-202609-009"].iloc[0]
    assert matched.reconciliation_status == MATCHED
    assert "receipt:physical_receipt.png" in matched.evidence_ids


def test_confirmed_receipt_does_not_double_count_spend():
    bundle = load_sample_bundle()
    amazon = confirm_receipt(
        validate_extracted(
            {
                "merchant": "Amazon.com",
                "purchase_date": "2026-09-07",
                "order_id": "ORD-AMZ-1001",
                "total_amount": 42.16,
                "currency": "USD",
            },
            "amazon_receipt.eml",
            "email",
        )
    )
    grocery = confirm_receipt(
        validate_extracted(
            {
                "merchant": "Whole Foods Market #10555",
                "purchase_date": "2026-09-22",
                "total_amount": 72.40,
                "currency": "USD",
            },
            "physical_receipt.png",
            "image",
        )
    )
    result, summary = reconcile(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        bundle["bank"]["df"],
        evidence_files=["amazon_receipt.eml"],
        confirmed_receipts=[amazon, grocery],
    )
    posted = actual_posted_spend_cents(result)
    assert posted == to_cents(437.35)
    assert to_cents(summary["actual_posted_spending"]) == posted
    amazon_row = result.loc[result["transaction_id"] == "CC-202609-001"].iloc[0]
    assert amazon_row.reconciliation_status == MATCHED
    assert "receipt:amazon_receipt.eml" in amazon_row.evidence_ids
    grocery_row = result.loc[result["transaction_id"] == "CC-202609-009"].iloc[0]
    assert grocery_row.reconciliation_status == MATCHED
    txn_ids = result.loc[result["transaction_id"] != "", "transaction_id"]
    assert list(txn_ids) == list(dict.fromkeys(txn_ids))


def test_ambiguous_receipt_is_flagged_and_not_added_as_spend():
    cards = pd.DataFrame(
        [
            {
                "txn_id": "CC-A",
                "posted_date": "2026-09-18",
                "merchant_raw": "AMAZON.COM*AAA",
                "txn_type": "charge",
                "amount": 29.99,
            },
            {
                "txn_id": "CC-B",
                "posted_date": "2026-09-18",
                "merchant_raw": "AMAZON.COM*BBB",
                "txn_type": "charge",
                "amount": 29.99,
            },
        ]
    )
    receipt = confirm_receipt(
        validate_extracted(
            {
                "merchant": "Amazon.com",
                "purchase_date": "2026-09-18",
                "total_amount": 29.99,
                "currency": "USD",
            },
            "dup_receipt.eml",
            "email",
        )
    )
    result, summary = reconcile(cards, confirmed_receipts=[receipt])
    statuses = set(result["reconciliation_status"])
    assert NEEDS_REVIEW in statuses
    assert actual_posted_spend_cents(result) == to_cents(59.98)
    assert to_cents(summary["actual_posted_spending"]) == to_cents(59.98)
