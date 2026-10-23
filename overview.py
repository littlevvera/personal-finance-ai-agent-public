"""Static Overview landing page. No API calls and no uploaded data."""

from __future__ import annotations

import streamlit as st

NAV_TABS = (
    "Overview",
    "Upload Data",
    "Reconciliation",
    "Spending Insights",
    "Action Report",
)


def _icon(paths: str) -> str:
    return (
        '<svg class="ov-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
        f"{paths}</svg>"
    )


ICON_LAYERS = _icon(
    '<rect x="3.5" y="4.5" width="17" height="4" rx="1.2" stroke="#1769d1" stroke-width="1.7"/>'
    '<rect x="3.5" y="10" width="17" height="4" rx="1.2" stroke="#1769d1" stroke-width="1.7"/>'
    '<rect x="3.5" y="15.5" width="17" height="4" rx="1.2" stroke="#1769d1" stroke-width="1.7"/>'
)
ICON_MATCH = _icon(
    '<circle cx="7.5" cy="8" r="2.4" stroke="#1769d1" stroke-width="1.7"/>'
    '<circle cx="16.5" cy="16" r="2.4" stroke="#1769d1" stroke-width="1.7"/>'
    '<path d="M9.6 9.6 14.4 14.4" stroke="#1769d1" stroke-width="1.7" stroke-linecap="round"/>'
)
ICON_SEARCH = _icon(
    '<circle cx="11" cy="11" r="5.2" stroke="#1769d1" stroke-width="1.7"/>'
    '<path d="M15.2 15.2 20 20" stroke="#1769d1" stroke-width="1.7" stroke-linecap="round"/>'
)
ICON_CHART = _icon(
    '<path d="M4.5 19.5V5.5M4.5 19.5H19.5" stroke="#1769d1" stroke-width="1.7" stroke-linecap="round"/>'
    '<rect x="7.2" y="11.2" width="3" height="5.3" rx="0.6" fill="#1769d1"/>'
    '<rect x="11.6" y="8" width="3" height="8.5" rx="0.6" fill="#1769d1"/>'
    '<rect x="16" y="13" width="3" height="3.5" rx="0.6" fill="#1769d1"/>'
)
ICON_LIST = _icon(
    '<rect x="4" y="4.5" width="16" height="15" rx="2" stroke="#1769d1" stroke-width="1.7"/>'
    '<path d="M8 9.2h8M8 12.2h8M8 15.2h5" stroke="#1769d1" stroke-width="1.7" stroke-linecap="round"/>'
)
ICON_DOWNLOAD = _icon(
    '<path d="M12 5v9.5M8.2 11.2 12 15l3.8-3.8" stroke="#1769d1" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M5 18.5h14" stroke="#1769d1" stroke-width="1.7" stroke-linecap="round"/>'
)

FEATURES = (
    (
        ICON_LAYERS,
        "Unified financial data",
        "Upload card statements, bank data, online orders, email receipts, and receipt images in one workspace.",
    ),
    (
        ICON_MATCH,
        "Smart reconciliation",
        "Match posted transactions to supporting evidence and surface unexplained differences.",
    ),
    (
        ICON_SEARCH,
        "AI exception investigation",
        "Investigate discrepancies with real, read-only tool calls and retrieve supporting evidence.",
    ),
    (
        ICON_CHART,
        "Spending intelligence",
        "Review posted spending by category and merchant from the reconciled ledger.",
    ),
    (
        ICON_LIST,
        "Actionable recommendations",
        "See which issues need review, with merchant-support guidance only when it is verified.",
    ),
    (
        ICON_DOWNLOAD,
        "Downloadable reports",
        "Generate a monthly financial summary and an exception report you can keep offline.",
    ),
)

SOURCES = (
    "Credit card statements",
    "Bank statements",
    "Amazon and Costco purchase histories",
    "Email purchase confirmations",
    "Physical store receipts",
)

PAINS = (
    "Repetitive cross-platform searches",
    "Manual transaction matching",
    "Difficulty identifying missing refunds",
    "Potential duplicate charges",
    "Inconsistent merchant descriptions",
    "Fragmented spending visibility",
)

STEPS = (
    "Download bank and credit card statements",
    "Search online orders and receipts",
    "Manually match charges, purchases, and refunds",
    "Investigate discrepancies and missing records",
    "Categorize spending and prepare a summary",
)


def overview_html() -> str:
    sources = "".join(f'<li>{item}</li>' for item in SOURCES)
    pains = "".join(f'<div class="ov-pain">{item}</div>' for item in PAINS)
    steps = "".join(
        f'<div class="ov-step"><span class="ov-step-num">{index}</span>'
        f'<p>{text}</p></div>'
        for index, text in enumerate(STEPS, start=1)
    )
    cards = "".join(
        f'<article class="ov-card">{icon}<h3>{title}</h3><p>{body}</p></article>'
        for icon, title, body in FEATURES
    )
    return f"""
<div class="ov-page">
  <section class="ov-hero">
    <p class="ov-eyebrow">Personal finance intelligence</p>
    <h1>Make sense of every transaction.</h1>
    <p class="ov-lede">Reconcile statements, uncover discrepancies, and understand your spending — all in one place, powered by AI.</p>
  </section>

  <section class="ov-section">
    <h2>The problem: fragmented financial records</h2>
    <p class="ov-prose">Reviewing personal financial activity is an essential, recurring task. The records live in different places, so the same month has to be reconstructed by hand.</p>
    <ul class="ov-sources">{sources}</ul>
    <p class="ov-prose">The usual process means checking charges, matching purchases, verifying refunds, investigating discrepancies, and categorizing spending across disconnected sources.</p>
    <div class="ov-pains">{pains}</div>
  </section>

  <section class="ov-section">
    <h2>The manual workflow</h2>
    <div class="ov-steps">{steps}</div>
    <p class="ov-note">The repetitive investigation and cross-referencing steps are the main opportunities for automation. Statement totals and posted amounts still need deterministic processing, not guesswork.</p>
  </section>

  <section class="ov-section ov-split">
    <h2>One platform. A smarter workflow.</h2>
    <div class="ov-pillars">
      <article class="ov-pillar">
        <h3>Deterministic processing</h3>
        <p>Python performs the financial calculations and transaction matching, so posted amounts and match status stay exact.</p>
      </article>
      <article class="ov-pillar">
        <h3>AI where it helps</h3>
        <p>AI extracts information from receipts, investigates exceptions with read-only tools, and writes evidence-grounded recommendations.</p>
      </article>
      <article class="ov-pillar">
        <h3>Human review</h3>
        <p>Uncertain results, and any action that contacts an external party, stay with you. The agent does not change financial records.</p>
      </article>
    </div>
  </section>

  <section class="ov-section">
    <h2>Key features</h2>
    <div class="ov-cards">{cards}</div>
  </section>

  <section class="ov-cta">
    <h2>Ready to review your finances?</h2>
    <p>Head to Upload Data to get started with your own files or the included synthetic sample data.</p>
  </section>
</div>
"""


def render_overview_tab() -> None:
    st.markdown(overview_html(), unsafe_allow_html=True)
    st.button(
        "Go to Upload Data",
        type="primary",
        on_click=_open_upload_tab,
        key="overview_go_upload",
    )
    st.caption("Opens the Upload Data tab. Sample files are included if you do not want to use your own.")


def _open_upload_tab() -> None:
    st.session_state.main_nav = "Upload Data"
