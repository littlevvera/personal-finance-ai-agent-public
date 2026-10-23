# LedgerMatch

A personal purchase-reconciliation and spending-insights agent. Households upload synthetic (or local) card and bank statements, Amazon and Costco orders, email receipts, and paper-receipt photos. Python matches posted money without double-counting. Optional OpenAI calls extract receipts, investigate exceptions, and summarize spend. The monthly report is previewed and downloaded from Streamlit. There is no Gmail, SMTP, or other email sending.

All sample money is fictional. Do not use private statements.

Company-specific identifiers in this public repository have been redacted for privacy. Development prompt logs are kept as a technical record; redacted prompt text is not a word-for-word original.

## What the app does

1. **Upload Data** — credit card CSV, bank CSV, online orders, `.eml` receipts, JPG/PNG receipts.
2. **Extract receipts** — AI extraction stays draft until the user confirms it.
3. **Reconciliation** — deterministic matching in `reconciliation.py` (integer cents).
4. **Investigate** — optional read-only OpenAI tools on one exception at a time.
5. **Spending Insights** — posted spend by category and merchant; optional AI commentary on aggregates only.
6. **Action Report** — generate, preview, and download an HTML monthly report plus a CSV of exceptions. Nothing is emailed.

## Financial rules

- Statements are the ledger. Orders and receipts are evidence, not extra spend.
- Card autopay and internal bank transfers are not spend.
- Suspected duplicate charges stay in actual posted spending until reversed; exposure is shown separately.
- Posted card refunds reduce net spending. Order refunds with no card refund are not subtracted.
- Category math is Python. The LLM does not add amounts.

## How to run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Optional AI features read `OPENAI_API_KEY` from a project `.env` file or Streamlit secrets. The key is never shown in the UI.

## Sample data

Reporting month: **September 2026**.

| File | Role |
|---|---|
| `sample_data/credit_card.csv` | Posted card ledger |
| `sample_data/bank.csv` | Posted bank ledger (payroll, card autopay, savings transfer) |
| `sample_data/online_orders.csv` | Amazon/Costco line-item evidence. Use one `order_total` per `order_id`. |
| `sample_data/amazon_receipt.eml` | Email evidence for `ORD-AMZ-1001` / `CC-202609-001` at `$42.16` |
| `sample_data/physical_receipt.png` | Synthetic Whole Foods ticket for `CC-202609-009` at `$72.40` |
| `sample_data/ground_truth.json` | Intended matches, exceptions, and spend counted once |

Posted sample card charges sum to **$437.35**. Duplicate exposure is **$29.99**. Card payment **$1,204.00** is a transfer.

## Out of scope

- Bank login, Gmail, SMTP, OAuth, or sending email
- Databases
- Auto-disputing charges
- Treating Amazon or Costco history as spend in addition to the card
- Inventing missing receipts, fees, or month-over-month trends

## Tests

```bash
python -m pytest -q
```
