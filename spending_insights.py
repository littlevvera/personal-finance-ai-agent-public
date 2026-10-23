"""One-shot spending insights from verified aggregates. Does not investigate exceptions."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from openai import APIError, APITimeoutError, OpenAI

from secrets_loader import MISSING_KEY_MESSAGE, get_openai_api_key
from spending_analytics import insight_payload

INSIGHTS_PROMPT = """You are LedgerMatch Spending Analyst. You receive verified monthly spending aggregates computed in Python.

Rules:
- Treat the JSON as facts. Do not invent amounts, merchants, categories, refunds, or percentages.
- Quote only numbers present in the payload. If a figure is missing, say it is unavailable.
- Label facts versus suggestions. Suggestions must not include dollar savings estimates unless the payload already contains that figure.
- Do not claim month-over-month or year-over-year changes. Only one period is provided.
- Do not treat orders or receipts as extra spend. Posted card/bank purchases are the spend ledger.
- Mention limitations from warnings, uncategorized amounts, unmatched purchases, and unverified refunds.
- Do not claim fraud. Do not say you emailed a merchant or changed records.
- Return a single JSON object with keys:
  period,
  largest_categories_summary,
  merchant_concentration,
  recommendations (array of short actionable strings grounded in these numbers),
  unresolved_risks (array),
  further_review (array),
  limitations (array),
  fact_versus_suggestion_note
- Keep prose concise. No markdown.
"""

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 30.0

RESULT_FIELDS = [
    "period",
    "largest_categories_summary",
    "merchant_concentration",
    "recommendations",
    "unresolved_risks",
    "further_review",
    "limitations",
    "fact_versus_suggestion_note",
]


class SpendingInsightsError(Exception):
    """Raised when insights generation fails."""


def _extract_json(text: str) -> dict:
    if not text or not str(text).strip():
        raise SpendingInsightsError("The model returned an empty insights response.")
    blob = str(text).strip()
    match = re.search(r"\{.*\}", blob, flags=re.DOTALL)
    if match:
        blob = match.group(0)
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError as exc:
        raise SpendingInsightsError("The model did not return valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise SpendingInsightsError("The model JSON was not an object.")
    return parsed


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


def _normalize(raw: dict, period: str) -> dict:
    result = {field: raw.get(field) for field in RESULT_FIELDS}
    result["period"] = result.get("period") or period
    for field in ("recommendations", "unresolved_risks", "further_review", "limitations"):
        value = result.get(field)
        if value is None:
            result[field] = []
        elif isinstance(value, str):
            result[field] = [value]
        elif isinstance(value, list):
            result[field] = [str(item) for item in value if item]
        else:
            result[field] = []
    for field in (
        "largest_categories_summary",
        "merchant_concentration",
        "fact_versus_suggestion_note",
    ):
        if result.get(field) is None:
            result[field] = ""
        else:
            result[field] = str(result[field])
    return result


def generate_spending_insights(
    report: dict,
    *,
    client: Optional[Any] = None,
) -> dict:
    payload = insight_payload(report)
    if client is None:
        key = get_openai_api_key()
        if not key:
            raise SpendingInsightsError(MISSING_KEY_MESSAGE)
        client = OpenAI(api_key=key, timeout=DEFAULT_TIMEOUT)
    user_content = (
        INSIGHTS_PROMPT
        + "\n---BEGIN VERIFIED AGGREGATES---\n"
        + json.dumps(payload, default=str)
        + "\n---END VERIFIED AGGREGATES---"
    )
    try:
        response = client.responses.create(
            model=DEFAULT_MODEL,
            input=[{"role": "user", "content": user_content}],
            max_output_tokens=700,
        )
    except APITimeoutError as exc:
        raise SpendingInsightsError("The OpenAI request timed out.") from exc
    except APIError as exc:
        raise SpendingInsightsError("OpenAI API error while generating spending insights.") from exc
    except Exception as exc:
        raise SpendingInsightsError("Spending insights generation failed.") from exc
    parsed = _extract_json(_output_text(response))
    return _normalize(parsed, str(payload.get("period") or ""))
