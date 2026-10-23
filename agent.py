"""Single investigation agent using the OpenAI Responses API and function calling."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from openai import APIError, APITimeoutError, OpenAI

from secrets_loader import MISSING_KEY_MESSAGE, get_openai_api_key
from tools import ALLOWED_TOOLS, TOOL_DEFINITIONS, InvestigationToolkit

SYSTEM_PROMPT = """You are LedgerMatch Investigator, a single read-only exception analyst for personal purchase reconciliation.

You receive one reconciliation exception. You may call at most three tools in total:
1. get_transaction_details(transaction_id)
2. find_related_orders(merchant, amount, date)
3. get_merchant_support(merchant)

Rules:
- Call tools only when you need records you do not already have.
- Use tool results as facts. Anything not returned by a tool or present in the exception payload is a hypothesis. Label hypotheses explicitly.
- Cite real transaction_id and order_id values from tools or the exception. Never invent ids, fees, tax lines, phone numbers, or support URLs.
- get_merchant_support may only echo official Amazon or Costco URLs from the tool. If the tool returns no URL, leave merchant_support_url empty.
- Never claim confirmed fraud. Never tell the user you contacted a merchant. Never change financial records.
- If evidence is missing or conflicting, set review_status to needs_review.
- If contacting a merchant is appropriate and a support URL exists, draft a short customer_service_message that cites posted amounts and ids. Otherwise leave customer_service_message empty.
- After tools (or immediately if tools are unnecessary), respond with a single JSON object and no markdown. Keys:
  exception_id, issue_type, investigation_summary, supporting_evidence_ids,
  possible_root_cause, recommended_action, merchant_support_url,
  customer_service_message, review_status
- review_status must be needs_review or ready_for_user_action.
- supporting_evidence_ids must be an array of strings.
"""

RESULT_FIELDS = [
    "exception_id",
    "issue_type",
    "investigation_summary",
    "supporting_evidence_ids",
    "possible_root_cause",
    "recommended_action",
    "merchant_support_url",
    "customer_service_message",
    "review_status",
]

MAX_TOOL_CALLS = 3
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 30.0


class InvestigationError(Exception):
    """Raised when the model cannot complete a valid investigation."""


def _needs_review(exception: dict, reason: str, evidence: Optional[list] = None) -> dict:
    return {
        "exception_id": str(exception.get("exception_id") or ""),
        "issue_type": str(exception.get("reconciliation_status") or "NEEDS_REVIEW"),
        "investigation_summary": reason,
        "supporting_evidence_ids": evidence or [],
        "possible_root_cause": "Insufficient reliable evidence to conclude.",
        "recommended_action": "Keep the exception open for manual review. Do not treat this as confirmed merchant error or fraud.",
        "merchant_support_url": "",
        "customer_service_message": "",
        "review_status": "needs_review",
    }


def _extract_json(text: str) -> dict:
    if not text or not str(text).strip():
        raise InvestigationError("The model returned an empty response.")
    blob = str(text).strip()
    match = re.search(r"\{.*\}", blob, flags=re.DOTALL)
    if match:
        blob = match.group(0)
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise InvestigationError("The model did not return valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise InvestigationError("The model JSON was not an object.")
    return parsed


def _validate_result(raw: dict, exception: dict) -> dict:
    out = {}
    for field in RESULT_FIELDS:
        value = raw.get(field, "")
        out[field] = value
    out["exception_id"] = str(out["exception_id"] or exception.get("exception_id") or "")
    out["issue_type"] = str(out["issue_type"] or exception.get("reconciliation_status") or "")
    if not isinstance(out["supporting_evidence_ids"], list):
        out["supporting_evidence_ids"] = [str(out["supporting_evidence_ids"])]
    out["supporting_evidence_ids"] = [str(item) for item in out["supporting_evidence_ids"] if str(item).strip()]
    summary = str(out.get("investigation_summary") or "").strip()
    if not summary:
        raise InvestigationError("investigation_summary was missing.")
    if "confirmed fraud" in summary.lower() or "is fraud" in str(out.get("possible_root_cause", "")).lower():
        out["possible_root_cause"] = "Do not treat this as confirmed fraud. " + str(out.get("possible_root_cause") or "")
        out["review_status"] = "needs_review"
    status = str(out.get("review_status") or "").strip()
    if status not in {"needs_review", "ready_for_user_action"}:
        out["review_status"] = "needs_review"
    out["merchant_support_url"] = str(out.get("merchant_support_url") or "")
    if out["merchant_support_url"] and "amazon.com" not in out["merchant_support_url"] and "costco.com" not in out["merchant_support_url"]:
        out["merchant_support_url"] = ""
        if out["review_status"] != "needs_review":
            out["recommended_action"] = (
                str(out.get("recommended_action") or "")
                + " Support URL omitted because it was not a verified Amazon or Costco page."
            ).strip()
    out["customer_service_message"] = str(out.get("customer_service_message") or "")
    for key in RESULT_FIELDS:
        if key != "supporting_evidence_ids":
            out[key] = "" if out[key] is None else str(out[key])
    return out


def _output_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return str(text)
    chunks = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) != "message":
            continue
        for part in getattr(item, "content", []) or []:
            part_text = getattr(part, "text", None)
            if part_text:
                chunks.append(part_text)
    return "\n".join(chunks)


def _function_calls(response: Any) -> list:
    calls = []
    for item in getattr(response, "output", []) or []:
        if getattr(item, "type", None) == "function_call":
            calls.append(item)
    return calls


def exception_to_payload(row: Any) -> dict:
    if hasattr(row, "_asdict"):
        data = row._asdict()
    elif hasattr(row, "to_dict"):
        data = row.to_dict()
    else:
        data = dict(row)
    clean = {}
    for key, value in data.items():
        if value is None:
            clean[key] = None
        elif hasattr(value, "item") and not isinstance(value, (bytes, str)):
            try:
                value = value.item()
            except Exception:
                pass
        try:
            import pandas as pd

            if pd.isna(value):
                clean[key] = None
                continue
        except Exception:
            pass
        if hasattr(value, "isoformat"):
            clean[key] = value.isoformat()
        else:
            clean[key] = value
    return clean


class InvestigationAgent:
    def __init__(
        self,
        toolkit: InvestigationToolkit,
        *,
        client: Optional[Any] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.toolkit = toolkit
        self.model = model or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
        self.timeout = timeout
        if client is not None:
            self.client = client
        else:
            key = api_key or get_openai_api_key()
            if not key:
                raise InvestigationError(MISSING_KEY_MESSAGE)
            self.client = OpenAI(api_key=key, timeout=timeout)

    def _create(self, conversation: list, *, allow_tools: bool) -> Any:
        kwargs = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": conversation,
            "max_output_tokens": 800,
        }
        if allow_tools:
            kwargs["tools"] = TOOL_DEFINITIONS
        try:
            return self.client.responses.create(**kwargs)
        except APITimeoutError as exc:
            raise InvestigationError("The OpenAI request timed out.") from exc
        except APIError as exc:
            raise InvestigationError(f"OpenAI API error: {exc}") from exc
        except Exception as exc:
            raise InvestigationError(f"OpenAI request failed: {exc}") from exc

    def investigate(self, exception: dict) -> tuple[dict, list]:
        payload = exception_to_payload(exception)
        conversation: list[Any] = [
            {
                "role": "user",
                "content": (
                    "Investigate this reconciliation exception. Use tools if needed, "
                    "then return only the JSON object.\n"
                    + json.dumps(payload, default=str)
                ),
            }
        ]
        trace: list[dict] = []
        tools_used = 0
        allow_tools = True

        for _ in range(MAX_TOOL_CALLS + 1):
            response = self._create(conversation, allow_tools=allow_tools)
            output_items = list(getattr(response, "output", []) or [])
            calls = _function_calls(response)
            if not calls:
                try:
                    parsed = _validate_result(_extract_json(_output_text(response)), payload)
                except InvestigationError as exc:
                    return _needs_review(payload, str(exc)), trace
                return parsed, trace

            conversation.extend(output_items)
            remaining_budget = MAX_TOOL_CALLS - tools_used
            for index, call in enumerate(calls):
                name = str(getattr(call, "name", "") or "")
                call_id = str(getattr(call, "call_id", "") or "")
                raw_args = getattr(call, "arguments", "{}")
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    arguments = {}
                    result = {"ok": False, "error": "Tool arguments were not valid JSON."}
                else:
                    if tools_used >= MAX_TOOL_CALLS or index >= remaining_budget:
                        result = {
                            "ok": False,
                            "error": f"Tool call limit of {MAX_TOOL_CALLS} reached. Conclude from evidence already retrieved.",
                        }
                    elif name not in ALLOWED_TOOLS:
                        result = {"ok": False, "error": f"Tool {name} is not allowed."}
                    else:
                        result = self.toolkit.execute(name, arguments)
                        tools_used += 1
                trace.append(
                    {
                        "tool": name,
                        "arguments": arguments if isinstance(arguments, dict) else {},
                        "result": result,
                    }
                )
                conversation.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(result, default=str),
                    }
                )
            if tools_used >= MAX_TOOL_CALLS:
                allow_tools = False
                conversation.append(
                    {
                        "role": "user",
                        "content": "Tool call limit reached. Return the JSON result now using only retrieved evidence.",
                    }
                )

        return _needs_review(payload, "The agent exceeded the investigation loop without a final answer."), trace


def investigate_exception(exception: dict, toolkit: InvestigationToolkit, **kwargs) -> tuple[dict, list]:
    try:
        agent = InvestigationAgent(toolkit, **kwargs)
        return agent.investigate(exception)
    except InvestigationError as exc:
        return _needs_review(exception, str(exc)), []
