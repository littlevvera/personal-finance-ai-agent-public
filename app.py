"""Personal Finance AI Agent — upload and preview only in this phase."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from agent import investigate_exception
from loaders import CSV_PREVIEW_ROWS, FileLoadError, load_sample_bundle, read_csv
from receipt_extraction import (
    ReceiptExtractionError,
    confirm_receipt,
    extract_email_receipt,
    extract_image_receipt,
    parse_email_bytes,
)
from reconciliation import EXCEPTION_STATUSES, reconcile
from secrets_loader import MISSING_KEY_MESSAGE, get_openai_api_key
from report import generate_monthly_report
from spending_analytics import analyze_spending
from spending_insights import SpendingInsightsError, generate_spending_insights
from overview import NAV_TABS, render_overview_tab
from tools import InvestigationToolkit

APP_TITLE = "Personal Finance AI Agent"
PRIMARY_BLUE = "#1769d1"
NAVY = "#15345b"
MUTED = "#64748b"
BORDER = "#dce6f2"
SURFACE = "#f5f9ff"
CHART_HEIGHT = 320
CHART_COLORS = ["#1769d1", "#3d8ee0", "#5ba3e8", "#7eb8ee", "#a3cdf4"]
APP_CSS_PATH = Path(__file__).resolve().parent / ".streamlit" / "app.css"


def apply_visual_theme() -> None:
    st.html(APP_CSS_PATH)


def money_column(label: str) -> st.column_config.NumberColumn:
    return st.column_config.NumberColumn(label, format="$%.2f")


def show_table(frame: pd.DataFrame, money_columns: tuple[str, ...] = ()) -> None:
    config = {}
    for name in money_columns:
        if name in frame.columns:
            config[name] = money_column(name)
    st.dataframe(frame, width="stretch", hide_index=True, column_config=config)


def _axis(**extra):
    import altair as alt

    defaults = dict(
        labelFontSize=15,
        titleFontSize=15,
        labelColor=NAVY,
        titleColor=NAVY,
        domainColor=BORDER,
        gridColor=BORDER,
        labelOverlap=False,
        labelLimit=220,
        labelPadding=6,
    )
    defaults.update(extra)
    return alt.Axis(**defaults)


def category_charts(plot_df: pd.DataFrame):
    import altair as alt

    bar = (
        alt.Chart(plot_df)
        .mark_bar(color=PRIMARY_BLUE, cornerRadiusEnd=3)
        .encode(
            x=alt.X(
                "category:N",
                sort="-y",
                title="Category",
                scale=alt.Scale(paddingInner=0.35, paddingOuter=0.15),
                axis=_axis(labelAngle=0, labelAlign="center", labelBaseline="top"),
            ),
            y=alt.Y("amount:Q", title="Amount", axis=_axis(format="$,.2f", tickCount=5, grid=True)),
            tooltip=[
                alt.Tooltip("category:N", title="Category"),
                alt.Tooltip("amount:Q", title="Amount", format="$,.2f"),
                alt.Tooltip("percent:Q", title="Percent", format=".2f"),
            ],
        )
        .properties(height=CHART_HEIGHT)
        .configure_view(strokeWidth=0)
        .configure_axisX(labelAngle=0)
    )
    pie = (
        alt.Chart(plot_df)
        .mark_arc(innerRadius=62, stroke="#ffffff", strokeWidth=1.5)
        .encode(
            theta=alt.Theta("amount:Q", stack=True),
            color=alt.Color(
                "category:N",
                title="Category",
                scale=alt.Scale(range=CHART_COLORS),
                legend=alt.Legend(
                    labelFontSize=13,
                    titleFontSize=13,
                    labelColor=NAVY,
                    titleColor=NAVY,
                    orient="right",
                ),
            ),
            tooltip=[
                alt.Tooltip("category:N", title="Category"),
                alt.Tooltip("amount:Q", title="Amount", format="$,.2f"),
                alt.Tooltip("percent:Q", title="Percent", format=".2f"),
            ],
        )
        .properties(height=CHART_HEIGHT)
        .configure_view(strokeWidth=0)
    )
    bar_spec = bar.to_dict()
    bar_spec.setdefault("config", {}).setdefault("axisX", {})["labelAngle"] = 0
    left, right = st.columns(2, gap="large")
    left.vega_lite_chart(bar_spec, width="stretch", theme=None)
    right.vega_lite_chart(pie.to_dict(), width="stretch", theme=None)


def merchant_chart(merchants: pd.DataFrame) -> None:
    import altair as alt

    plot = merchants[["merchant", "amount", "percent", "transaction_count"]].copy()
    chart = (
        alt.Chart(plot)
        .mark_bar(color=PRIMARY_BLUE, cornerRadiusEnd=3)
        .encode(
            y=alt.Y(
                "merchant:N",
                sort="-x",
                title=None,
                axis=_axis(labelAlign="right", labelLimit=260),
            ),
            x=alt.X("amount:Q", title="Amount", axis=_axis(format="$,.2f", tickCount=5, grid=True)),
            tooltip=[
                alt.Tooltip("merchant:N", title="Merchant"),
                alt.Tooltip("amount:Q", title="Amount", format="$,.2f"),
                alt.Tooltip("percent:Q", title="Percent", format=".2f"),
                alt.Tooltip("transaction_count:Q", title="Transactions"),
            ],
        )
        .properties(height=CHART_HEIGHT)
        .configure_view(strokeWidth=0)
    )
    st.vega_lite_chart(chart.to_dict(), width="stretch", theme=None)


def init_state() -> None:
    defaults = {
        "credit_card": None,
        "bank": None,
        "online_orders": None,
        "email_receipts": [],
        "physical_receipts": [],
        "report_email": "",
        "upload_generation": 0,
        "csv_errors": {},
        "recon_result": None,
        "recon_summary": None,
        "recon_error": "",
        "investigations": {},
        "spending_insights": None,
        "monthly_report": None,
        "main_nav": "Overview",
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def source_ready(value) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return len(value) > 0
    return True


def ingest_csv(uploaded, *, state_key: str) -> None:
    if uploaded is None:
        return
    try:
        frame = read_csv(uploaded, source_name=uploaded.name)
    except FileLoadError as exc:
        st.session_state.csv_errors[state_key] = str(exc)
        return
    st.session_state.csv_errors.pop(state_key, None)
    st.session_state[state_key] = {
        "df": frame,
        "filename": uploaded.name,
        "origin": "upload",
    }


def render_csv_section(
    *,
    title: str,
    help_text: str,
    state_key: str,
    uploader_key: str,
) -> None:
    st.subheader(title)
    st.caption(help_text)
    uploaded = st.file_uploader(
        f"Upload {title} CSV",
        type=["csv"],
        key=uploader_key,
        label_visibility="collapsed",
    )
    ingest_csv(uploaded, state_key=state_key)

    error = st.session_state.csv_errors.get(state_key)
    if error:
        st.error(error)

    payload = st.session_state.get(state_key)
    if not payload:
        st.info(f"No {title.lower()} loaded yet.")
        return

    frame: pd.DataFrame = payload["df"]
    rows, cols = frame.shape
    origin = payload.get("origin", "upload")
    st.success(
        f"{payload['filename']} · {rows} rows × {cols} columns · source: {origin}"
    )
    preview = frame.head(CSV_PREVIEW_ROWS)
    money_cols = tuple(
        name for name in preview.columns if str(name).lower() in {"amount", "transaction_amount", "order_amount", "order_total", "line_total", "refund_amount"}
    )
    show_table(preview, money_cols)
    if rows > CSV_PREVIEW_ROWS:
        st.caption(f"Showing the first {CSV_PREVIEW_ROWS} of {rows} rows.")


def _items_to_text(items) -> str:
    lines = []
    for item in items or []:
        desc = item.get("description") or ""
        amount = item.get("amount")
        amount_text = "" if amount is None else str(amount)
        lines.append(f"{desc} | {amount_text}".strip())
    return "\n".join(lines)


def _items_from_text(text: str) -> list[dict]:
    items = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "|" in line:
            desc, amount = line.split("|", 1)
        else:
            desc, amount = line, ""
        items.append({"description": desc.strip(), "amount": amount.strip() or None})
    return items


def confirmed_receipts_from_session() -> list[dict]:
    confirmed = []
    for group in ("email_receipts", "physical_receipts"):
        for item in st.session_state.get(group) or []:
            extraction = item.get("extraction")
            if item.get("confirmed") and extraction:
                confirmed.append(dict(extraction))
    return confirmed


def render_receipt_review(item: dict, *, kind: str, index: int) -> None:
    filename = item["filename"]
    st.markdown(f"**{filename}** · source: {item.get('origin', 'upload')}")
    if kind == "email":
        payload = item.get("bytes")
        if payload:
            try:
                preview = parse_email_bytes(payload, filename)
                st.text_area(
                    "Email preview",
                    preview[:4000],
                    height=160,
                    key=f"email_preview_{index}_{filename}",
                    disabled=True,
                )
            except ReceiptExtractionError as exc:
                st.warning(str(exc))
        else:
            st.caption("No email bytes are stored for this file, so it cannot be extracted.")
    else:
        if item.get("bytes"):
            st.image(item["bytes"], caption=filename, width="stretch")

    extract_key = f"extract_{kind}_{index}_{filename}"
    if st.button("Extract receipt with AI", key=extract_key):
        if not get_openai_api_key():
            item["extract_error"] = MISSING_KEY_MESSAGE
        elif not item.get("bytes"):
            item["extract_error"] = f"{filename} has no stored file bytes."
        else:
            try:
                if kind == "email":
                    extracted = extract_email_receipt(item["bytes"], filename)
                else:
                    extracted = extract_image_receipt(item["bytes"], filename)
                item["extraction"] = extracted
                item["extract_error"] = ""
                item["confirmed"] = False
            except ReceiptExtractionError as exc:
                item["extract_error"] = str(exc)
                item["confirmed"] = False

    if item.get("extract_error"):
        st.error(item["extract_error"])

    extraction = item.get("extraction")
    if not extraction:
        if item.get("confirmed"):
            st.success("This receipt is confirmed.")
        else:
            st.caption("Extracted values are not verified until you confirm them.")
        return

    for warning in extraction.get("warnings") or []:
        st.warning(warning)

    form_key = f"form_{kind}_{index}_{filename}"
    with st.form(form_key):
        st.caption("Correct any extracted values before confirming. Unconfirmed receipts are not used in matching.")
        merchant = st.text_input("Merchant", value=extraction.get("merchant") or "", key=f"{form_key}_merchant")
        purchase_date = st.text_input(
            "Purchase date",
            value=extraction.get("purchase_date") or "",
            key=f"{form_key}_date",
        )
        order_id = st.text_input("Order id", value=extraction.get("order_id") or "", key=f"{form_key}_oid")
        currency = st.text_input("Currency", value=extraction.get("currency") or "", key=f"{form_key}_ccy")
        cols = st.columns(3)
        subtotal = cols[0].text_input(
            "Subtotal",
            value="" if extraction.get("subtotal") is None else str(extraction.get("subtotal")),
            key=f"{form_key}_sub",
        )
        sales_tax = cols[1].text_input(
            "Sales tax",
            value="" if extraction.get("sales_tax") is None else str(extraction.get("sales_tax")),
            key=f"{form_key}_tax",
        )
        total_amount = cols[2].text_input(
            "Total amount",
            value="" if extraction.get("total_amount") is None else str(extraction.get("total_amount")),
            key=f"{form_key}_tot",
        )
        items_text = st.text_area(
            "Items (description | amount)",
            value=_items_to_text(extraction.get("items")),
            height=120,
            key=f"{form_key}_items",
        )
        submitted = st.form_submit_button("Confirm receipt")

    if submitted:
        item["extraction"] = confirm_receipt(
            extraction,
            {
                "merchant": merchant,
                "purchase_date": purchase_date,
                "order_id": order_id,
                "currency": currency,
                "subtotal": subtotal,
                "sales_tax": sales_tax,
                "total_amount": total_amount,
                "items": _items_from_text(items_text),
            },
        )
        item["confirmed"] = True
        st.success(
            f"Confirmed {filename}. Re-run reconciliation to use this as supporting evidence. "
            "Original card and bank rows were not changed."
        )
    elif item.get("confirmed"):
        st.success(f"{filename} is confirmed for reconciliation evidence.")
    else:
        st.info("Extracted values are draft only until you click Confirm receipt.")


def render_status() -> None:
    st.subheader("Upload status")
    rows = [
        ("Credit card statement", source_ready(st.session_state.credit_card)),
        ("Bank statement", source_ready(st.session_state.bank)),
        ("Online purchase history", source_ready(st.session_state.online_orders)),
        ("Email receipts", source_ready(st.session_state.email_receipts)),
        ("Physical receipts", source_ready(st.session_state.physical_receipts)),
    ]
    status_df = pd.DataFrame(
        {
            "Data source": [name for name, _ in rows],
            "Provided": ["Yes" if ready else "No" for _, ready in rows],
        }
    )
    show_table(status_df)


def load_samples() -> None:
    try:
        bundle = load_sample_bundle()
    except FileLoadError as exc:
        st.session_state.csv_errors["sample"] = str(exc)
        return
    st.session_state.csv_errors.pop("sample", None)
    st.session_state.credit_card = bundle["credit_card"]
    st.session_state.bank = bundle["bank"]
    st.session_state.online_orders = bundle["online_orders"]
    st.session_state.email_receipts = bundle["email_receipts"]
    st.session_state.physical_receipts = bundle["physical_receipts"]
    st.session_state.upload_generation += 1
    st.session_state.recon_result = None
    st.session_state.recon_summary = None
    st.session_state.recon_error = ""
    st.session_state.investigations = {}
    st.session_state.spending_insights = None
    st.session_state.monthly_report = None


def render_upload_tab() -> None:
    st.header("Upload Data")
    st.write(
        "Load a credit card statement, bank statement, Amazon or Costco purchase "
        "history, email receipts, and photos of paper receipts. Files stay in this "
        "browser session only. Nothing is written to a database."
    )

    sample_col, _ = st.columns([1, 2])
    with sample_col:
        if st.button("Load Sample Data", type="primary"):
            load_samples()
            st.rerun()

    if st.session_state.csv_errors.get("sample"):
        st.error(st.session_state.csv_errors["sample"])

    render_status()
    st.divider()

    gen = st.session_state.upload_generation
    render_csv_section(
        title="Credit Card Statement",
        help_text="CSV export from a credit card. Preview only; charges are not matched yet.",
        state_key="credit_card",
        uploader_key=f"upload_credit_card_{gen}",
    )
    st.divider()
    render_csv_section(
        title="Bank Statement",
        help_text="CSV export from a bank account. Card autopay should appear here as a transfer, not as extra spend.",
        state_key="bank",
        uploader_key=f"upload_bank_{gen}",
    )
    st.divider()
    render_csv_section(
        title="Online Purchase History",
        help_text="CSV from Amazon or Costco online orders. This is evidence, not a second ledger.",
        state_key="online_orders",
        uploader_key=f"upload_orders_{gen}",
    )
    st.divider()

    st.subheader("Email Receipts")
    st.caption(
        "Accepts .eml files. Extracted fields stay draft until you confirm them. "
        "Receipt text is treated as untrusted data."
    )
    email_files = st.file_uploader(
        "Upload email receipts",
        type=["eml"],
        accept_multiple_files=True,
        key=f"upload_email_{gen}",
        label_visibility="collapsed",
    )
    if email_files:
        st.session_state.email_receipts = [
            {
                "filename": item.name,
                "origin": "upload",
                "size_bytes": item.size if item.size is not None else len(item.getvalue()),
                "bytes": item.getvalue(),
                "extraction": None,
                "extract_error": "",
                "confirmed": False,
            }
            for item in email_files
        ]

    receipts = st.session_state.email_receipts
    if receipts:
        st.success(f"{len(receipts)} email receipt file(s) loaded.")
        listing = pd.DataFrame(
            {
                "Filename": [item["filename"] for item in receipts],
                "Size (bytes)": [item.get("size_bytes", len(item.get("bytes") or b"")) for item in receipts],
                "Source": [item["origin"] for item in receipts],
                "Confirmed": ["Yes" if item.get("confirmed") else "No" for item in receipts],
            }
        )
        show_table(listing)
        for index, item in enumerate(receipts):
            render_receipt_review(item, kind="email", index=index)
    else:
        st.info("No email receipts loaded yet.")

    st.divider()
    st.subheader("Physical Receipts")
    st.caption(
        "Accepts JPG, JPEG, and PNG. Use Extract receipt with AI, correct the form, then Confirm receipt. "
        "Unconfirmed extractions are not reconciliation evidence."
    )
    image_files = st.file_uploader(
        "Upload physical receipts",
        type=["jpg", "jpeg", "png"],
        accept_multiple_files=True,
        key=f"upload_images_{gen}",
        label_visibility="collapsed",
    )
    if image_files:
        st.session_state.physical_receipts = [
            {
                "filename": item.name,
                "origin": "upload",
                "bytes": item.getvalue(),
                "extraction": None,
                "extract_error": "",
                "confirmed": False,
            }
            for item in image_files
        ]

    photos = st.session_state.physical_receipts
    if photos:
        st.success(f"{len(photos)} physical receipt image(s) loaded.")
        for index, photo in enumerate(photos):
            render_receipt_review(photo, kind="image", index=index)
    else:
        st.info("No physical receipts loaded yet.")


def _loaded_frame(state_key: str):
    payload = st.session_state.get(state_key)
    if not payload:
        return None
    return payload["df"]


def render_reconciliation_tab() -> None:
    st.header("Reconciliation")
    st.write(
        "Compare posted card lines with Amazon and Costco orders. "
        "Matching is deterministic Python. Exception investigation may call OpenAI "
        "with read-only tools. The agent never changes financial records."
    )
    card_df = _loaded_frame("credit_card")
    orders_df = _loaded_frame("online_orders")
    bank_df = _loaded_frame("bank")
    if card_df is None:
        st.warning("Load a credit card statement on the Upload Data tab first.")
        return

    if st.button("Run Reconciliation", type="primary"):
        try:
            evidence = [item["filename"] for item in st.session_state.email_receipts]
            result, summary = reconcile(
                card_df,
                orders_df,
                bank_df,
                evidence_files=evidence,
                confirmed_receipts=confirmed_receipts_from_session(),
            )
            st.session_state.recon_result = result
            st.session_state.recon_summary = summary
            st.session_state.recon_error = ""
            st.session_state.investigations = {}
            st.session_state.spending_insights = None
            st.session_state.monthly_report = None
        except Exception as exc:
            st.session_state.recon_result = None
            st.session_state.recon_summary = None
            st.session_state.recon_error = str(exc)
            st.session_state.investigations = {}
            st.session_state.spending_insights = None
            st.session_state.monthly_report = None

    if st.session_state.recon_error:
        st.error(st.session_state.recon_error)
        return
    if st.session_state.recon_summary is None:
        st.info("No reconciliation has been run in this session.")
        return

    summary = st.session_state.recon_summary
    metric_cols = st.columns(3)
    metric_cols[0].metric(
        "Total transactions analyzed",
        summary["total_transactions_analyzed"],
    )
    metric_cols[1].metric("Matched transactions", summary["matched_transactions"])
    metric_cols[2].metric(
        "Exceptions requiring review",
        summary["exceptions_requiring_review"],
    )
    spend_cols = st.columns(3)
    spend_cols[0].metric(
        "Actual posted spending",
        f"${summary['actual_posted_spending']:.2f}",
    )
    spend_cols[1].metric(
        "Potential duplicate exposure",
        f"${summary['potential_duplicate_exposure']:.2f}",
    )
    spend_cols[2].metric(
        "Provisional adjusted spending",
        f"${summary['provisional_adjusted_spending']:.2f}",
    )
    st.caption(
        "Actual posted spending is every card charge still on the statement, including "
        "suspected duplicates. Duplicate exposure is not removed from that total until "
        "the issuer reverses it. Provisional adjusted spending subtracts that exposure. "
        "CARD_PAYMENT / bank autopay is a transfer and is never spend. "
        "Order totals and confirmed receipt totals are not added on top of card charges. "
        "Only user-confirmed receipts are attached as evidence."
    )

    result: pd.DataFrame = st.session_state.recon_result
    st.subheader("Reconciliation results")
    show_table(
        result,
        (
            "transaction_amount",
            "order_amount",
            "difference",
            "group_transaction_amount",
            "group_order_amount",
            "group_difference",
        ),
    )

    exceptions = result[result["reconciliation_status"].isin(EXCEPTION_STATUSES)].copy()
    st.subheader("Exception explanations")
    if exceptions.empty:
        st.success("No exceptions requiring review.")
        return
    for _, row in exceptions.iterrows():
        st.markdown(
            f"**{row['exception_id']} · {row['reconciliation_status']} · "
            f"{row['transaction_id'] or '(no card line)'}**"
        )
        st.write(row["explanation"])

    st.subheader("Investigate with AI")
    st.caption(
        "The agent can call at most three read-only tools. It does not email merchants "
        "or edit the statement. The OpenAI key is loaded from a local .env file or "
        "Streamlit secrets and is never shown here."
    )

    labels = [
        f"{row.exception_id} · {row.reconciliation_status} · {row.transaction_id or 'no card line'}"
        for row in exceptions.itertuples(index=False)
    ]
    chosen = st.selectbox("Select an exception", labels)
    selected_row = exceptions.iloc[labels.index(chosen)].to_dict()

    if st.button("Investigate with AI"):
        if not get_openai_api_key():
            st.error(MISSING_KEY_MESSAGE)
        else:
            toolkit = InvestigationToolkit(
                card_df,
                orders_df,
                recon_df=result,
                email_receipts=st.session_state.email_receipts,
            )
            findings, trace = investigate_exception(selected_row, toolkit)
            st.session_state.investigations[selected_row["exception_id"]] = {
                "result": findings,
                "trace": trace,
            }

    saved = st.session_state.investigations.get(selected_row["exception_id"])
    if not saved:
        st.info("No AI investigation has been run for this exception in this session.")
        return

    findings = saved["result"]
    st.markdown(f"**{findings.get('exception_id')} · {findings.get('issue_type')} · {findings.get('review_status')}**")
    st.write(findings.get("investigation_summary"))
    st.write(f"Possible root cause: {findings.get('possible_root_cause')}")
    st.write(f"Recommended action: {findings.get('recommended_action')}")
    evidence = findings.get("supporting_evidence_ids") or []
    st.write("Supporting evidence: " + (", ".join(evidence) if evidence else "none"))
    url = findings.get("merchant_support_url") or ""
    if url:
        st.write(f"Official merchant support: {url}")
    else:
        st.write("Official merchant support: none verified for this merchant.")
    draft = findings.get("customer_service_message") or ""
    if draft:
        st.text_area("Customer service message draft", draft, height=120)
    else:
        st.caption("No customer service draft. The agent did not recommend contacting a merchant.")

    with st.expander("Agent investigation trace"):
        st.caption("Actual tool calls from this run. Empty if the model returned JSON without tools or the API failed before any call.")
        st.json(saved["trace"])


def render_insights_tab() -> None:
    st.header("Spending Insights")
    st.write(
        "Monthly posted spending from reconciled card and bank activity. "
        "Orders and receipts explain charges; they are not added as extra spend. "
        "Python computes every total. AI insights use those aggregates only."
    )
    card_df = _loaded_frame("credit_card")
    if card_df is None or st.session_state.recon_result is None:
        st.warning("Run reconciliation on the Reconciliation tab first.")
        return

    report = analyze_spending(
        st.session_state.recon_result,
        card_df=card_df,
        orders_df=_loaded_frame("online_orders"),
        bank_df=_loaded_frame("bank"),
        confirmed_receipts=confirmed_receipts_from_session(),
    )

    failed_checks = [item for item in report["checks"] if not item["ok"]]
    if failed_checks:
        for item in failed_checks:
            st.error(item["message"])
    for warning in report["warnings"]:
        st.warning(warning)

    st.subheader("Executive summary")
    st.caption(f"Reporting period: {report['period']}")
    row1 = st.columns(3)
    row1[0].metric("Total posted spending", f"${report['posted_spending']:.2f}")
    row1[1].metric("Total net spending", f"${report['net_spending']:.2f}")
    row1[2].metric("Open exceptions", report["exceptions_requiring_review"])
    row2 = st.columns(3)
    row2[0].metric("Top spending category", report["top_category"] or "n/a")
    row2[1].metric("Top spending merchant", report["top_merchant"] or "n/a")
    row2[2].metric("Potential duplicate exposure", f"${report['duplicate_exposure']:.2f}")
    st.caption(
        f"{report['transactions_analyzed']} card transactions analyzed · "
        f"{report['unmatched_transactions']} unmatched posted purchases · "
        f"posted refunds ${report['posted_refunds']:.2f} · "
        f"uncategorized ${report['uncategorized']:.2f} ({report['uncategorized_percent']:.2f}%)"
    )

    st.subheader("Spending by category")
    cat_frame = pd.DataFrame(report["categories"])
    if cat_frame.empty:
        st.info("No categorized spending.")
    else:
        display = cat_frame[["category", "amount", "percent"]].rename(
            columns={"category": "Category", "amount": "Total amount", "percent": "Percent of posted spending"}
        )
        show_table(display, ("Total amount",))
        category_charts(cat_frame[["category", "amount", "percent"]].copy())

    st.subheader("Merchant analysis")
    merchants = pd.DataFrame(report["top_merchants"])
    if merchants.empty:
        st.info("No merchant spending to rank.")
    else:
        merch_table = merchants.rename(
            columns={
                "merchant": "Merchant",
                "amount": "Total amount",
                "percent": "Percent of spending",
                "transaction_count": "Number of transactions",
            }
        )[["Merchant", "Total amount", "Percent of spending", "Number of transactions"]]
        table_col, chart_col = st.columns(2, gap="large")
        with table_col:
            show_table(merch_table, ("Total amount",))
        with chart_col:
            merchant_chart(merchants)

    st.subheader("Transaction details")
    txns = pd.DataFrame(report["transactions"])
    if txns.empty:
        st.info("No spending transactions.")
    else:
        dates = sorted({item for item in txns["transaction_date"].tolist() if item})
        categories = sorted({part.strip() for cell in txns["spending_category"] for part in str(cell).split(",") if part.strip()})
        merchants_opts = sorted({item for item in txns["merchant"].tolist() if item})
        statuses = sorted({item for item in txns["reconciliation_status"].tolist() if item})
        filters = st.columns(4)
        date_choice = filters[0].selectbox("Date range", ["All"] + dates)
        cat_choice = filters[1].selectbox("Category", ["All"] + categories)
        merch_choice = filters[2].selectbox("Merchant", ["All"] + merchants_opts)
        status_choice = filters[3].selectbox("Reconciliation status", ["All"] + statuses)
        filtered = txns
        if date_choice != "All":
            filtered = filtered[filtered["transaction_date"] == date_choice]
        if cat_choice != "All":
            filtered = filtered[filtered["spending_category"].str.contains(cat_choice, regex=False)]
        if merch_choice != "All":
            filtered = filtered[filtered["merchant"] == merch_choice]
        if status_choice != "All":
            filtered = filtered[filtered["reconciliation_status"] == status_choice]
        show_table(
            filtered[
                [
                    "transaction_date",
                    "merchant",
                    "amount",
                    "spending_category",
                    "reconciliation_status",
                    "supporting_source_ids",
                ]
            ].rename(
                columns={
                    "transaction_date": "Transaction date",
                    "merchant": "Merchant",
                    "amount": "Amount",
                    "spending_category": "Spending category",
                    "reconciliation_status": "Reconciliation status",
                    "supporting_source_ids": "Supporting source ids",
                }
            ),
            ("Amount",),
        )

    st.subheader("AI spending insights")
    st.caption(
        "Sends verified totals, category mix, merchant ranks, and exception summaries. "
        "Does not send raw statements or the API key. One model call per click."
    )
    if st.button("Generate AI spending insights"):
        if not get_openai_api_key():
            st.error(MISSING_KEY_MESSAGE)
        else:
            try:
                st.session_state.spending_insights = generate_spending_insights(report)
            except SpendingInsightsError as exc:
                st.session_state.spending_insights = {"error": str(exc)}

    saved = st.session_state.spending_insights
    if not saved:
        st.info("No AI spending insights have been generated in this session.")
        return
    if saved.get("error"):
        st.error(saved["error"])
        return
    st.write(saved.get("largest_categories_summary"))
    st.write(saved.get("merchant_concentration"))
    st.caption(saved.get("fact_versus_suggestion_note") or "")
    st.markdown("**Recommendations**")
    for item in saved.get("recommendations") or []:
        st.write(f"- {item}")
    st.markdown("**Unresolved transaction risks**")
    for item in saved.get("unresolved_risks") or []:
        st.write(f"- {item}")
    st.markdown("**Further review**")
    for item in saved.get("further_review") or []:
        st.write(f"- {item}")
    st.markdown("**Limitations**")
    for item in saved.get("limitations") or []:
        st.write(f"- {item}")


def render_report_tab() -> None:
    st.header("Action Report")
    st.write(
        "Generate a monthly financial report from the current reconciliation, "
        "session investigations, and any spending insights already produced. "
        "Nothing is emailed. Download the HTML report or the exception CSV."
    )
    if st.session_state.recon_result is None:
        st.warning("Run reconciliation on the Reconciliation tab first.")
        return

    if st.button("Generate report", type="primary"):
        payload = generate_monthly_report(
            st.session_state.recon_result,
            card_df=_loaded_frame("credit_card"),
            orders_df=_loaded_frame("online_orders"),
            bank_df=_loaded_frame("bank"),
            confirmed_receipts=confirmed_receipts_from_session(),
            investigations=st.session_state.investigations,
            spending_insights=st.session_state.spending_insights,
            recon_summary=st.session_state.recon_summary,
        )
        st.session_state.monthly_report = payload

    saved = st.session_state.monthly_report
    if not saved:
        st.info("No report has been generated in this session.")
        return
    if not saved.get("complete"):
        st.error(saved.get("error") or "The report is incomplete.")
    else:
        st.success("Report generated from this session’s reconciliation. Investigations were not rerun.")

    stem = saved.get("filename_stem") or "ledgermatch-report"
    downloads = st.columns(2)
    with downloads[0]:
        st.download_button(
            "Download HTML report",
            data=saved["html"].encode("utf-8"),
            file_name=f"{stem}.html",
            mime="text/html",
        )
    with downloads[1]:
        st.download_button(
            "Download CSV exception report",
            data=saved["csv"].encode("utf-8"),
            file_name=f"{stem}-exceptions.csv",
            mime="text/csv",
        )

    st.subheader("Preview report")
    st.components.v1.html(saved["html"], height=900, scrolling=True)


def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    apply_visual_theme()
    init_state()
    st.html(
        '<p id="app-attribution" style="margin:0 0 0.55rem 0;color:#1769d1;'
        "font-size:17px;font-weight:600;line-height:1.45;letter-spacing:0.01em;"
        'display:block;visibility:visible;opacity:1;">'
        "Built by Wei for AI Solutions.</p>"
    )
    st.title(APP_TITLE)
    st.markdown(
        "<p class='app-kicker'>AI-Powered Purchase Reconciliation &amp; Spending Insights</p>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<p class='app-intro'>Reconcile card and bank statements against Amazon, Costco, email, "
        "and paper receipts. Use the tabs below to upload, match, analyze, and download a monthly report.</p>",
        unsafe_allow_html=True,
    )

    selected = st.pills(
        "Workspace tabs",
        NAV_TABS,
        key="main_nav",
        label_visibility="collapsed",
        width="stretch",
    )
    page = selected or "Overview"
    if page == "Overview":
        render_overview_tab()
    elif page == "Upload Data":
        render_upload_tab()
    elif page == "Reconciliation":
        render_reconciliation_tab()
    elif page == "Spending Insights":
        render_insights_tab()
    else:
        render_report_tab()


if __name__ == "__main__":
    main()
