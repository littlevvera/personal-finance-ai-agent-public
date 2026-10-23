"""Standalone HTML/CSV monthly report. Reuses recon and spending analytics. No email."""

from __future__ import annotations

import csv
import html
import io
from typing import Optional

import pandas as pd

from reconciliation import (
    AMOUNT_MISMATCH,
    EXCEPTION_STATUSES,
    MATCHED,
    MATCHED_SPLIT,
    POSSIBLE_DUPLICATE,
    REFUND_NOT_FOUND,
    UNMATCHED,
    cents_to_dollars,
    to_cents,
)
from spending_analytics import analyze_spending

DISCLOSURE = (
    "This report uses synthetic demo data for LedgerMatch. Identifiers, merchants, "
    "and amounts are fictional. It is not tax, legal, or investment advice. "
    "Orders and receipts are evidence, not a second spend ledger."
)
NOT_INVESTIGATED = "not investigated"


def _esc(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _money(value) -> str:
    return f"${cents_to_dollars(to_cents(value)):,.2f}"


def _count_status(recon_df: pd.DataFrame, status: str) -> int:
    if recon_df is None or recon_df.empty:
        return 0
    return int((recon_df["reconciliation_status"] == status).sum())


def _investigation_for(exception_id: str, investigations: Optional[dict]) -> dict:
    stored = (investigations or {}).get(exception_id)
    if not stored:
        return {"status": NOT_INVESTIGATED}
    payload = stored.get("result") if isinstance(stored, dict) and "result" in stored else stored
    if not isinstance(payload, dict) or not payload:
        return {"status": NOT_INVESTIGATED}
    return {
        "status": "investigated",
        "investigation_summary": payload.get("investigation_summary") or "",
        "possible_root_cause": payload.get("possible_root_cause") or "",
        "supporting_evidence_ids": payload.get("supporting_evidence_ids") or [],
        "review_status": payload.get("review_status") or "",
        "recommended_action": payload.get("recommended_action") or "",
        "merchant_support_url": payload.get("merchant_support_url") or "",
        "customer_service_message": payload.get("customer_service_message") or "",
        "issue_type": payload.get("issue_type") or "",
    }


def exceptions_csv(
    recon_df: pd.DataFrame,
    investigations: Optional[dict] = None,
) -> str:
    if recon_df is None or recon_df.empty:
        return "exception_id,transaction_id,reconciliation_status,investigation_status\n"
    rows = recon_df[recon_df["reconciliation_status"].isin(EXCEPTION_STATUSES)].copy()
    buffer = io.StringIO()
    fieldnames = [
        "exception_id",
        "transaction_id",
        "order_id",
        "merchant",
        "transaction_amount",
        "order_amount",
        "difference",
        "reconciliation_status",
        "evidence_ids",
        "explanation",
        "investigation_status",
        "review_status",
        "possible_root_cause",
        "supporting_evidence_ids",
    ]
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for _, row in rows.iterrows():
        inv = _investigation_for(str(row.get("exception_id") or ""), investigations)
        evidence = inv.get("supporting_evidence_ids") or []
        if isinstance(evidence, list):
            evidence_text = ";".join(str(item) for item in evidence)
        else:
            evidence_text = str(evidence)
        writer.writerow(
            {
                "exception_id": row.get("exception_id") or "",
                "transaction_id": row.get("transaction_id") or "",
                "order_id": row.get("order_id") or "",
                "merchant": row.get("merchant") or "",
                "transaction_amount": row.get("transaction_amount") if pd.notna(row.get("transaction_amount")) else "",
                "order_amount": row.get("order_amount") if pd.notna(row.get("order_amount")) else "",
                "difference": row.get("difference") if pd.notna(row.get("difference")) else "",
                "reconciliation_status": row.get("reconciliation_status") or "",
                "evidence_ids": row.get("evidence_ids") or "",
                "explanation": row.get("explanation") or "",
                "investigation_status": inv.get("status"),
                "review_status": inv.get("review_status") or "",
                "possible_root_cause": inv.get("possible_root_cause") or "",
                "supporting_evidence_ids": evidence_text,
            }
        )
    return buffer.getvalue()


def generate_monthly_report(
    recon_df: Optional[pd.DataFrame] = None,
    *,
    card_df: Optional[pd.DataFrame] = None,
    orders_df: Optional[pd.DataFrame] = None,
    bank_df: Optional[pd.DataFrame] = None,
    confirmed_receipts: Optional[list] = None,
    investigations: Optional[dict] = None,
    spending_insights: Optional[dict] = None,
    recon_summary: Optional[dict] = None,
) -> dict:
    if recon_df is None or getattr(recon_df, "empty", True):
        html_doc = _wrap_html(
            "LedgerMatch monthly report",
            "<p class='warn'>No reconciliation result is available. Run reconciliation before generating a report. Missing values were not invented.</p>",
        )
        return {
            "complete": False,
            "error": "No reconciliation result is available.",
            "html": html_doc,
            "csv": exceptions_csv(pd.DataFrame()),
            "filename_stem": "ledgermatch-report",
        }

    spend = analyze_spending(
        recon_df,
        card_df=card_df,
        orders_df=orders_df,
        bank_df=bank_df,
        confirmed_receipts=confirmed_receipts or [],
    )
    summary = recon_summary or {}
    matched = int(summary.get("matched_transactions") or _count_status(recon_df, MATCHED) + _count_status(recon_df, MATCHED_SPLIT))
    body = _render_body(recon_df, spend, investigations or {}, spending_insights, matched)
    period = spend.get("period") or "unknown"
    stem = f"ledgermatch-report-{period.replace(' ', '-')}"
    return {
        "complete": True,
        "error": "",
        "html": _wrap_html(f"LedgerMatch monthly report — {period}", body),
        "csv": exceptions_csv(recon_df, investigations),
        "filename_stem": stem,
        "spend": spend,
    }


def _chart_height(n: int) -> int:
    return min(320, max(260, 200 + max(n, 1) * 22))


def _svg_vertical_bars(items: list[dict], *, label_key: str, value_key: str, title: str) -> str:
    if not items:
        return ""
    width = 700
    height = _chart_height(len(items))
    left, right, top, bottom = 56, 16, 28, 52
    plot_w = width - left - right
    plot_h = height - top - bottom
    values = [float(item.get(value_key) or 0) for item in items]
    peak = max(values) if values else 1.0
    peak = peak if peak > 0 else 1.0
    gap = 0.32
    slot = plot_w / max(len(items), 1)
    bar_w = slot * (1 - gap)
    bars = []
    labels = []
    for index, item in enumerate(items):
        value = float(item.get(value_key) or 0)
        bar_h = (value / peak) * plot_h
        x = left + index * slot + slot * gap / 2
        y = top + plot_h - bar_h
        bars.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{max(bar_h, 0.5):.1f}" '
            f'fill="#1769d1" rx="3"/>'
        )
        lx = x + bar_w / 2
        labels.append(
            f'<text x="{lx:.1f}" y="{height - 18}" text-anchor="middle" class="chart-label">'
            f'{_esc(item.get(label_key))}</text>'
        )
    ticks = []
    for frac in (0, 0.5, 1.0):
        ty = top + plot_h - frac * plot_h
        ticks.append(
            f'<line x1="{left}" x2="{width - right}" y1="{ty:.1f}" y2="{ty:.1f}" class="chart-grid"/>'
            f'<text x="{left - 8}" y="{ty + 3:.1f}" text-anchor="end" class="chart-label">'
            f'{_money(peak * frac)}</text>'
        )
    return (
        f'<figure class="chart">'
        f'<figcaption>{_esc(title)}</figcaption>'
        f'<svg viewBox="0 0 {width} {height}" width="700" height="{height}" '
        f'role="img" aria-label="{_esc(title)}">'
        f'{"".join(ticks)}{"".join(bars)}{"".join(labels)}</svg></figure>'
    )


def _svg_horizontal_bars(items: list[dict], *, label_key: str, value_key: str, title: str) -> str:
    if not items:
        return ""
    width = 700
    height = _chart_height(len(items))
    left, right, top, bottom = 168, 72, 16, 28
    plot_w = width - left - right
    plot_h = height - top - bottom
    values = [float(item.get(value_key) or 0) for item in items]
    peak = max(values) if values else 1.0
    peak = peak if peak > 0 else 1.0
    slot = plot_h / max(len(items), 1)
    bar_h = min(22, slot * 0.62)
    bars = []
    for index, item in enumerate(items):
        value = float(item.get(value_key) or 0)
        bar_w = (value / peak) * plot_w
        y = top + index * slot + (slot - bar_h) / 2
        label = str(item.get(label_key) or "")
        if len(label) > 28:
            label = label[:26] + "…"
        value_x = left + max(bar_w, 0.5) + 6
        bars.append(
            f'<text x="{left - 8}" y="{y + bar_h * 0.75:.1f}" text-anchor="end" class="chart-label">'
            f'{_esc(label)}</text>'
            f'<rect x="{left}" y="{y:.1f}" width="{max(bar_w, 0.5):.1f}" height="{bar_h:.1f}" '
            f'fill="#1769d1" rx="3"/>'
            f'<text x="{value_x:.1f}" y="{y + bar_h * 0.75:.1f}" class="chart-label">'
            f'{_money(value)}</text>'
        )
    return (
        f'<figure class="chart">'
        f'<figcaption>{_esc(title)}</figcaption>'
        f'<svg viewBox="0 0 {width} {height}" width="700" height="{height}" '
        f'role="img" aria-label="{_esc(title)}">'
        f'{"".join(bars)}</svg></figure>'
    )


def _list_html(items) -> str:
    values = [item for item in (items or []) if item]
    if not values:
        return "<p>None recorded.</p>"
    return "<ul>" + "".join(f"<li>{_esc(item)}</li>" for item in values) + "</ul>"


def _render_body(
    recon_df: pd.DataFrame,
    spend: dict,
    investigations: dict,
    spending_insights: Optional[dict],
    matched: int,
) -> str:
    exceptions = recon_df[recon_df["reconciliation_status"].isin(EXCEPTION_STATUSES)].copy()
    cat_rows = "".join(
        "<tr>"
        f"<td>{_esc(item['category'])}</td>"
        f"<td class='num'>{_money(item['amount'])}</td>"
        f"<td class='num'>{_esc(item['percent'])}%</td>"
        "</tr>"
        for item in spend.get("categories") or []
    )
    merch_rows = "".join(
        "<tr>"
        f"<td>{_esc(item['merchant'])}</td>"
        f"<td class='num'>{_money(item['amount'])}</td>"
        f"<td class='num'>{_esc(item['percent'])}%</td>"
        f"<td class='num'>{_esc(item['transaction_count'])}</td>"
        "</tr>"
        for item in spend.get("top_merchants") or []
    )

    recon_counts = [
        ("Matched transactions", matched),
        ("Split payments", _count_status(recon_df, MATCHED_SPLIT)),
        ("Amount mismatches", _count_status(recon_df, AMOUNT_MISMATCH)),
        ("Possible duplicate charges", _count_status(recon_df, POSSIBLE_DUPLICATE)),
        ("Missing refunds", _count_status(recon_df, REFUND_NOT_FOUND)),
        ("Unmatched transactions", _count_status(recon_df, UNMATCHED)),
        ("Open exceptions", int(spend.get("exceptions_requiring_review") or 0)),
    ]
    recon_rows = "".join(
        f"<tr><td>{_esc(label)}</td><td class='num'>{_esc(value)}</td></tr>"
        for label, value in recon_counts
    )

    inv_sections = []
    action_items = []
    for _, row in exceptions.iterrows():
        eid = str(row.get("exception_id") or "")
        inv = _investigation_for(eid, investigations)
        status_label = inv.get("status")
        facts = inv.get("investigation_summary") if status_label == "investigated" else "not investigated"
        cause = inv.get("possible_root_cause") if status_label == "investigated" else "not investigated"
        review = inv.get("review_status") if status_label == "investigated" else "not investigated"
        evidence = inv.get("supporting_evidence_ids") if status_label == "investigated" else []
        if isinstance(evidence, list):
            evidence_text = ", ".join(str(item) for item in evidence) if evidence else "none"
        else:
            evidence_text = str(evidence or "none")
        inv_sections.append(
            "<article class='card exception'>"
            f"<h3>{_esc(eid)} · {_esc(row.get('reconciliation_status'))}</h3>"
            f"<p><strong>Transaction:</strong> {_esc(row.get('transaction_id') or '(none)')} · "
            f"<strong>Merchant:</strong> {_esc(row.get('merchant'))} · "
            f"<strong>Amount:</strong> {_money(row.get('transaction_amount'))}</p>"
            f"<p class='wrap'><strong>Evidence ids:</strong> {_esc(row.get('evidence_ids'))}</p>"
            f"<p class='wrap'><strong>Confirmed facts:</strong> {_esc(facts)}</p>"
            f"<p class='wrap'><strong>Possible root cause:</strong> {_esc(cause)}</p>"
            f"<p class='wrap'><strong>Supporting evidence ids:</strong> {_esc(evidence_text)}</p>"
            f"<p><strong>Review status:</strong> {_esc(review)}</p>"
            "</article>"
        )
        if status_label != "investigated":
            action_items.append(
                f"{eid} ({row.get('reconciliation_status')}) requires human review. AI investigation was not run."
            )
        else:
            if inv.get("recommended_action"):
                action_items.append(f"{eid}: {inv['recommended_action']}")
            if inv.get("merchant_support_url"):
                action_items.append(f"{eid} merchant support: {inv['merchant_support_url']}")
            if inv.get("customer_service_message"):
                action_items.append(f"{eid} suggested customer service message: {inv['customer_service_message']}")

    insights = spending_insights if isinstance(spending_insights, dict) and not spending_insights.get("error") else None
    if insights:
        insights_html = (
            f"<p><strong>Key observations:</strong> {_esc(insights.get('merchant_concentration'))}</p>"
            f"<p><strong>Largest expense categories:</strong> {_esc(insights.get('largest_categories_summary'))}</p>"
            "<h3>Actionable recommendations</h3>"
            + _list_html(insights.get("recommendations"))
            + "<h3>Data limitations</h3>"
            + _list_html(insights.get("limitations"))
        )
        if insights.get("fact_versus_suggestion_note"):
            insights_html += f"<p>{_esc(insights.get('fact_versus_suggestion_note'))}</p>"
    else:
        insights_html = (
            "<p>AI spending insights were not generated in this session. "
            "This section was not invented.</p>"
        )

    warnings = spend.get("warnings") or []
    warning_html = "".join(f"<p class='warn'>{_esc(item)}</p>" for item in warnings)

    category_chart = _svg_vertical_bars(
        spend.get("categories") or [],
        label_key="category",
        value_key="amount",
        title="Spending by category",
    )
    merchant_chart = _svg_horizontal_bars(
        spend.get("top_merchants") or [],
        label_key="merchant",
        value_key="amount",
        title="Top merchants by spending",
    )
    return f"""
<header class="masthead">
  <p class="eyebrow">LedgerMatch</p>
  <h1>LedgerMatch monthly financial report</h1>
  <p class="period">Reporting period: {_esc(spend.get('period'))}</p>
  <p class="disclosure">{_esc(DISCLOSURE)}</p>
</header>
{warning_html}

<section class="section">
<h2>1. Executive summary</h2>
<table class="summary">
  <colgroup><col class="label-col"><col class="value-col"></colgroup>
  <tbody>
    <tr><th>Reporting period</th><td>{_esc(spend.get('period'))}</td></tr>
    <tr><th>Total posted spending</th><td class="num">{_money(spend.get('posted_spending'))}</td></tr>
    <tr><th>Total net spending</th><td class="num">{_money(spend.get('net_spending'))}</td></tr>
    <tr><th>Posted refunds</th><td class="num">{_money(spend.get('posted_refunds'))}</td></tr>
    <tr><th>Total transactions analyzed</th><td class="num">{_esc(spend.get('transactions_analyzed'))}</td></tr>
    <tr><th>Open exceptions</th><td class="num">{_esc(spend.get('exceptions_requiring_review'))}</td></tr>
  </tbody>
</table>
</section>

<section class="section">
<h2>2. Spending analysis</h2>
<p>Highest spending category: <strong>{_esc(spend.get('top_category') or 'n/a')}</strong>.
Uncategorized spending: <strong>{_money(spend.get('uncategorized'))}</strong>
({_esc(spend.get('uncategorized_percent'))}%).
Potential duplicate charge exposure: <strong>{_money(spend.get('duplicate_exposure'))}</strong>
(included in posted spending until reversed).</p>
<h3>Category breakdown</h3>
<table>
  <colgroup><col><col class="num-col"><col class="num-col"></colgroup>
  <thead><tr><th>Category</th><th class="num">Amount</th><th class="num">Percent of posted spending</th></tr></thead>
  <tbody>{cat_rows or '<tr><td colspan="3">No category totals.</td></tr>'}</tbody>
</table>
{category_chart}
<h3>Top five merchants</h3>
<table>
  <colgroup><col><col class="num-col"><col class="num-col"><col class="num-col"></colgroup>
  <thead><tr><th>Merchant</th><th class="num">Amount</th><th class="num">Percent</th><th class="num">Transactions</th></tr></thead>
  <tbody>{merch_rows or '<tr><td colspan="4">No merchant totals.</td></tr>'}</tbody>
</table>
{merchant_chart}
</section>

<section class="section">
<h2>3. Reconciliation results</h2>
<table>
  <colgroup><col><col class="num-col"></colgroup>
  <thead><tr><th>Result</th><th class="num">Count</th></tr></thead>
  <tbody>{recon_rows}</tbody>
</table>
<p>Card payments and internal transfers are excluded from spending. Online orders and receipts are evidence only and are not added on top of matched card charges.</p>
</section>

<section class="section exceptions">
<h2>4. AI investigation findings</h2>
{''.join(inv_sections) if inv_sections else '<p>No reconciliation exceptions in this period.</p>'}
</section>

<section class="section">
<h2>5. Recommended next actions</h2>
{_list_html(action_items)}
</section>

<section class="section">
<h2>6. AI spending insights</h2>
{insights_html}
</section>
"""


def _wrap_html(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_esc(title)}</title>
  <style>
    :root {{
      --bg: #ffffff;
      --navy: #15345b;
      --muted: #64748b;
      --blue: #1769d1;
      --border: #dce6f2;
      --surface: #f5f9ff;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ background: var(--bg); color: var(--navy); }}
    body {{
      font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif;
      font-size: 10.5pt;
      line-height: 1.45;
      margin: 0 auto;
      max-width: 760px;
      padding: 28px 24px 48px;
    }}
    h1 {{
      font-size: 24pt;
      font-weight: 650;
      line-height: 1.2;
      margin: 0 0 6px;
      color: var(--navy);
    }}
    h2 {{
      font-size: 16pt;
      font-weight: 650;
      margin: 0 0 10px;
      padding-bottom: 4px;
      border-bottom: 1px solid var(--border);
      color: var(--navy);
    }}
    h3 {{
      font-size: 12pt;
      font-weight: 650;
      margin: 14px 0 8px;
      color: var(--navy);
    }}
    p, li {{ font-size: 10.5pt; margin: 0 0 8px; }}
    .eyebrow {{
      color: var(--blue);
      font-size: 9pt;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      margin: 0 0 6px;
    }}
    .period, .disclosure {{ color: var(--muted); font-size: 10pt; }}
    .masthead {{ margin-bottom: 18px; break-after: avoid-page; page-break-after: avoid; }}
    .section {{ margin: 18px 0 8px; }}
    .exceptions {{ break-inside: auto; page-break-inside: auto; }}
    h2, h3 {{ break-after: avoid-page; page-break-after: avoid; }}
    table {{
      width: 100%;
      max-width: 100%;
      table-layout: fixed;
      break-inside: avoid-page;
      page-break-inside: avoid;
      border-collapse: collapse;
      margin: 0 0 12px;
      font-size: 9.5pt;
      background: var(--bg);
    }}
    th, td {{
      border: 1px solid var(--border);
      padding: 6px 8px;
      text-align: left;
      vertical-align: top;
      word-wrap: break-word;
      overflow-wrap: anywhere;
    }}
    th {{ background: var(--surface); color: var(--navy); font-weight: 650; }}
    tbody tr:nth-child(even) {{ background: var(--surface); }}
    td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }}
    col.num-col {{ width: 22%; }}
    col.label-col {{ width: 58%; }}
    col.value-col {{ width: 42%; }}
    .wrap {{ overflow-wrap: anywhere; word-break: break-word; }}
    .warn {{
      background: #fff7ed;
      border: 1px solid #fdba74;
      color: var(--navy);
      padding: 8px 10px;
      margin: 0 0 12px;
    }}
    .card {{
      background: var(--bg);
      border: 1px solid var(--border);
      padding: 10px 12px;
      margin: 0 0 10px;
      break-inside: avoid-page;
      page-break-inside: avoid;
    }}
    .chart {{
      margin: 0 0 14px;
      max-width: 700px;
      break-inside: avoid-page;
      page-break-inside: avoid;
    }}
    .chart figcaption {{
      font-size: 10pt;
      font-weight: 650;
      margin: 0 0 6px;
      color: var(--navy);
    }}
    svg {{ display: block; width: 100%; max-width: 700px; height: auto; }}
    .chart-label {{ fill: var(--navy); font-size: 11px; font-family: "Segoe UI", Arial, sans-serif; }}
    .chart-grid {{ stroke: var(--border); stroke-width: 1; }}
    ul {{ padding-left: 1.15rem; margin: 0 0 10px; }}
    @page {{ size: letter; margin: 0.7in; }}
    @media print {{
      html, body {{
        background: #ffffff;
        max-width: none;
        width: auto;
        padding: 0;
        margin: 0;
        overflow: visible;
      }}
      * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
      .no-print, nav, button {{ display: none !important; }}
      .section {{ margin: 12px 0; }}
      .exceptions {{ break-inside: auto; page-break-inside: auto; }}
      .exceptions .card {{ break-inside: avoid-page; page-break-inside: avoid; }}
      .chart, table {{ max-width: 100%; }}
      svg {{ max-width: 100%; width: 100%; height: auto; }}
      a {{ color: var(--navy); text-decoration: none; }}
    }}
    @media screen and (max-width: 800px) {{
      body {{ padding: 16px; }}
      h1 {{ font-size: 22pt; }}
    }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""
