import json
from types import SimpleNamespace

from agent import InvestigationAgent, SYSTEM_PROMPT, investigate_exception
from loaders import load_sample_bundle
from reconciliation import reconcile
from tools import InvestigationToolkit, VERIFIED_SUPPORT


def _exception(status: str):
    bundle = load_sample_bundle()
    result, _ = reconcile(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        bundle["bank"]["df"],
    )
    toolkit = InvestigationToolkit(
        bundle["credit_card"]["df"],
        bundle["online_orders"]["df"],
        recon_df=result,
        email_receipts=bundle["email_receipts"],
    )
    row = result.loc[result["reconciliation_status"] == status].iloc[0]
    return toolkit, row.to_dict()


class ScriptedClient:
    def __init__(self, waves):
        self.waves = list(waves)
        self.calls = []
        self.responses = SimpleNamespace()

        def create(**kwargs):
            self.calls.append(kwargs)
            if not self.waves:
                raise AssertionError("Unexpected extra Responses API call")
            return self.waves.pop(0)

        self.responses.create = create


def _fn_call(name, arguments, call_id):
    return SimpleNamespace(
        type="function_call",
        name=name,
        arguments=json.dumps(arguments),
        call_id=call_id,
    )


def _message(payload: dict):
    text = json.dumps(payload)
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text)],
            )
        ],
        output_text=text,
    )


def _wave(calls):
    return SimpleNamespace(output=calls, output_text="")


def test_system_prompt_forbids_fraud_and_limits_tools():
    assert "at most three tools" in SYSTEM_PROMPT
    assert "Never claim confirmed fraud" in SYSTEM_PROMPT
    assert "Never invent ids" in SYSTEM_PROMPT


def test_amount_mismatch_investigation_uses_real_ids():
    toolkit, exception = _exception("AMOUNT_MISMATCH")
    final = {
        "exception_id": exception["exception_id"],
        "issue_type": "AMOUNT_MISMATCH",
        "investigation_summary": "Card CC-202609-006 posted 54.99 versus order ORD-AMZ-1004 total 49.99. Fact: 5.00 gap. Hypothesis: tax or marketplace fee not in the order file.",
        "supporting_evidence_ids": ["CC-202609-006", "ORD-AMZ-1004"],
        "possible_root_cause": "Posted card amount exceeds the Amazon order total in evidence.",
        "recommended_action": "Ask Amazon Orders for an itemized invoice for AMZN MKTP US*QW9 on 2026-09-20.",
        "merchant_support_url": VERIFIED_SUPPORT["amazon"]["support_url"],
        "customer_service_message": "Hello, I am reconciling card charge CC-202609-006 for $54.99 on 2026-09-20 against order ORD-AMZ-1004 for $49.99. Please confirm the $5.00 difference.",
        "review_status": "ready_for_user_action",
    }
    client = ScriptedClient(
        [
            _wave(
                [
                    _fn_call("get_transaction_details", {"transaction_id": "CC-202609-006"}, "c1"),
                    _fn_call(
                        "find_related_orders",
                        {"merchant": "AMZN MKTP US*QW9", "amount": 54.99, "date": "2026-09-20"},
                        "c2",
                    ),
                    _fn_call("get_merchant_support", {"merchant": "AMZN MKTP US*QW9"}, "c3"),
                ]
            ),
            _message(final),
        ]
    )
    result, trace = InvestigationAgent(toolkit, client=client).investigate(exception)
    assert result["issue_type"] == "AMOUNT_MISMATCH"
    assert "CC-202609-006" in result["supporting_evidence_ids"]
    assert "ORD-AMZ-1004" in result["supporting_evidence_ids"]
    assert result["merchant_support_url"] == VERIFIED_SUPPORT["amazon"]["support_url"]
    assert len(trace) == 3
    assert trace[0]["result"]["ok"] is True
    assert trace[1]["result"]["ok"] is True
    assert "ORD-AMZ-1004" in {item["order_id"] for item in trace[1]["result"]["orders"]}
    assert client.calls[0]["tools"]
    assert "tools" not in client.calls[1]


def test_possible_duplicate_investigation():
    toolkit, exception = _exception("POSSIBLE_DUPLICATE")
    assert exception["transaction_id"] == "CC-202609-005"
    final = {
        "exception_id": exception["exception_id"],
        "issue_type": "POSSIBLE_DUPLICATE",
        "investigation_summary": "CC-202609-005 is a second posted 29.99 Amazon charge the same day as matched CC-202609-004 / ORD-AMZ-1003. It remains posted spend until reversed.",
        "supporting_evidence_ids": ["CC-202609-005", "CC-202609-004", "ORD-AMZ-1003"],
        "possible_root_cause": "Possible duplicate authorization or a second purchase without a second order row.",
        "recommended_action": "Compare the two references with Amazon before disputing.",
        "merchant_support_url": VERIFIED_SUPPORT["amazon"]["support_url"],
        "customer_service_message": "Hello, card lines CC-202609-004 and CC-202609-005 both posted $29.99 as AMAZON.COM*9X2Q on 2026-09-18. I only have order ORD-AMZ-1003. Please confirm whether one charge is a duplicate.",
        "review_status": "needs_review",
    }
    client = ScriptedClient(
        [
            _wave([_fn_call("get_transaction_details", {"transaction_id": "CC-202609-005"}, "c1")]),
            _wave(
                [
                    _fn_call(
                        "find_related_orders",
                        {"merchant": "AMAZON.COM*9X2Q", "amount": 29.99, "date": "2026-09-18"},
                        "c2",
                    )
                ]
            ),
            _message(final),
        ]
    )
    result, trace = InvestigationAgent(toolkit, client=client).investigate(exception)
    assert result["review_status"] == "needs_review"
    assert "CC-202609-005" in result["supporting_evidence_ids"]
    assert "ORD-AMZ-1003" in {item["order_id"] for item in trace[1]["result"]["orders"]}
    assert "confirmed fraud" not in result["investigation_summary"].lower()


def test_missing_refund_investigation():
    toolkit, exception = _exception("REFUND_NOT_FOUND")
    assert exception["transaction_id"] == "CC-202609-007"
    final = {
        "exception_id": exception["exception_id"],
        "issue_type": "REFUND_NOT_FOUND",
        "investigation_summary": "Order ORD-AMZ-1005 shows refund_issued 24.99 on 2026-09-25. Card CC-202609-007 is still a posted charge with no refund row.",
        "supporting_evidence_ids": ["CC-202609-007", "ORD-AMZ-1005"],
        "possible_root_cause": "Refund may post in a later cycle. This is not a confirmed merchant error.",
        "recommended_action": "Wait for the next statement, then ask Amazon if the refund still has not posted.",
        "merchant_support_url": VERIFIED_SUPPORT["amazon"]["support_url"],
        "customer_service_message": "Hello, order ORD-AMZ-1005 shows a $24.99 refund on 2026-09-25 but card charge CC-202609-007 remains posted. Please confirm refund status.",
        "review_status": "needs_review",
    }
    client = ScriptedClient(
        [
            _wave(
                [
                    _fn_call("get_transaction_details", {"transaction_id": "CC-202609-007"}, "c1"),
                    _fn_call(
                        "find_related_orders",
                        {"merchant": "AMAZON.COM*CASE4H", "amount": 24.99, "date": "2026-09-05"},
                        "c2",
                    ),
                ]
            ),
            _message(final),
        ]
    )
    result, trace = InvestigationAgent(toolkit, client=client).investigate(exception)
    refund_order = next(
        item for item in trace[1]["result"]["orders"] if item["order_id"] == "ORD-AMZ-1005"
    )
    assert refund_order["refund_amount"] == 24.99
    assert "not a confirmed merchant error" in result["possible_root_cause"].lower() or "later cycle" in result["possible_root_cause"].lower()
    assert result["review_status"] == "needs_review"


def test_unmatched_purchase_does_not_invent_contact():
    toolkit, exception = _exception("UNMATCHED")
    assert exception["transaction_id"] == "CC-202609-009"
    final = {
        "exception_id": exception["exception_id"],
        "issue_type": "UNMATCHED",
        "investigation_summary": "CC-202609-009 Whole Foods 72.40 has no matching order in the loaded files. No receipt was invented.",
        "supporting_evidence_ids": ["CC-202609-009"],
        "possible_root_cause": "Missing grocery receipt, not a reconstructed order.",
        "recommended_action": "Keep unmatched until a receipt is uploaded. Do not contact Amazon or Costco for this merchant.",
        "merchant_support_url": "",
        "customer_service_message": "",
        "review_status": "needs_review",
    }
    client = ScriptedClient(
        [
            _wave(
                [
                    _fn_call("get_transaction_details", {"transaction_id": "CC-202609-009"}, "c1"),
                    _fn_call(
                        "find_related_orders",
                        {"merchant": "WHOLEFDS #10555", "amount": 72.4, "date": "2026-09-22"},
                        "c2",
                    ),
                    _fn_call("get_merchant_support", {"merchant": "WHOLEFDS #10555"}, "c3"),
                ]
            ),
            _message(final),
        ]
    )
    result, trace = InvestigationAgent(toolkit, client=client).investigate(exception)
    assert trace[1]["result"]["orders"] == []
    assert trace[2]["result"]["support_url"] is None
    assert result["merchant_support_url"] == ""
    assert result["customer_service_message"] == ""
    assert result["review_status"] == "needs_review"


def test_api_error_returns_needs_review_without_trace_fabrication():
    toolkit, exception = _exception("UNMATCHED")

    class Boom:
        def __init__(self):
            self.responses = SimpleNamespace(create=self.create)

        def create(self, **kwargs):
            raise RuntimeError("network down")

    result, trace = investigate_exception(exception, toolkit, client=Boom())
    assert result["review_status"] == "needs_review"
    assert trace == []
    assert "network down" in result["investigation_summary"] or "failed" in result["investigation_summary"].lower()


def test_missing_api_configuration(monkeypatch):
    monkeypatch.setattr("agent.get_openai_api_key", lambda: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    toolkit, exception = _exception("UNMATCHED")
    result, trace = investigate_exception(exception, toolkit)
    assert result["review_status"] == "needs_review"
    assert trace == []
    summary = result["investigation_summary"]
    assert "OpenAI API key" in summary
    assert ".env" in summary
    assert "Secrets" in summary
    assert "sk-" not in summary.lower()
    dumped = json.dumps(result) + json.dumps(trace)
    assert "sk-" not in dumped.lower()


def test_rejects_non_official_support_url():
    toolkit, exception = _exception("AMOUNT_MISMATCH")
    bad = {
        "exception_id": exception["exception_id"],
        "issue_type": "AMOUNT_MISMATCH",
        "investigation_summary": "Gap exists.",
        "supporting_evidence_ids": ["CC-202609-006"],
        "possible_root_cause": "Unknown.",
        "recommended_action": "Call a random number.",
        "merchant_support_url": "https://example.com/fake-support",
        "customer_service_message": "Hi",
        "review_status": "ready_for_user_action",
    }
    client = ScriptedClient([_message(bad)])
    result, _ = InvestigationAgent(toolkit, client=client).investigate(exception)
    assert result["merchant_support_url"] == ""
