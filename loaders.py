"""Load and validate uploaded or sample financial files. No matching or AI."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
SAMPLE_DIR = ROOT / "sample_data"

SAMPLE_CREDIT_CARD = SAMPLE_DIR / "credit_card.csv"
SAMPLE_BANK = SAMPLE_DIR / "bank.csv"
SAMPLE_ONLINE_ORDERS = SAMPLE_DIR / "online_orders.csv"
SAMPLE_EMAIL_RECEIPT = SAMPLE_DIR / "amazon_receipt.eml"
SAMPLE_PHYSICAL_RECEIPT = SAMPLE_DIR / "physical_receipt.png"

CSV_PREVIEW_ROWS = 50


class FileLoadError(Exception):
    """Raised when a user file cannot be shown as a table."""


def read_csv(file_obj, *, source_name: str) -> pd.DataFrame:
    """Read a CSV from a path, buffer, or Streamlit uploaded file."""
    try:
        frame = pd.read_csv(file_obj)
    except pd.errors.EmptyDataError as exc:
        raise FileLoadError(
            f"{source_name} is empty. Export the statement as CSV and try again."
        ) from exc
    except pd.errors.ParserError as exc:
        raise FileLoadError(
            f"{source_name} is not a readable CSV. Check the delimiter and header row."
        ) from exc
    except UnicodeDecodeError as exc:
        raise FileLoadError(
            f"{source_name} is not valid UTF-8 text. Save the file as CSV (UTF-8) and upload again."
        ) from exc
    except Exception as exc:
        raise FileLoadError(
            f"{source_name} could not be read as CSV: {exc}"
        ) from exc

    if frame.shape[1] == 0:
        raise FileLoadError(
            f"{source_name} has no columns. Confirm the file is a comma-separated statement export."
        )
    return frame


def load_sample_bundle() -> dict:
    """Load the synthetic September 2026 files from sample_data/. Does not invent rows."""
    missing = [
        path.name
        for path in (
            SAMPLE_CREDIT_CARD,
            SAMPLE_BANK,
            SAMPLE_ONLINE_ORDERS,
            SAMPLE_EMAIL_RECEIPT,
            SAMPLE_PHYSICAL_RECEIPT,
        )
        if not path.exists()
    ]
    if missing:
        raise FileLoadError(
            "Sample files are missing from sample_data/: " + ", ".join(missing)
        )

    email_bytes = SAMPLE_EMAIL_RECEIPT.read_bytes()
    return {
        "credit_card": {
            "df": read_csv(SAMPLE_CREDIT_CARD, source_name="credit_card.csv"),
            "filename": SAMPLE_CREDIT_CARD.name,
            "origin": "sample",
        },
        "bank": {
            "df": read_csv(SAMPLE_BANK, source_name="bank.csv"),
            "filename": SAMPLE_BANK.name,
            "origin": "sample",
        },
        "online_orders": {
            "df": read_csv(SAMPLE_ONLINE_ORDERS, source_name="online_orders.csv"),
            "filename": SAMPLE_ONLINE_ORDERS.name,
            "origin": "sample",
        },
        "email_receipts": [
            {
                "filename": SAMPLE_EMAIL_RECEIPT.name,
                "origin": "sample",
                "size_bytes": len(email_bytes),
                "bytes": email_bytes,
                "extraction": None,
                "extract_error": "",
                "confirmed": False,
            }
        ],
        "physical_receipts": [
            {
                "filename": SAMPLE_PHYSICAL_RECEIPT.name,
                "origin": "sample",
                "bytes": SAMPLE_PHYSICAL_RECEIPT.read_bytes(),
                "extraction": None,
                "extract_error": "",
                "confirmed": False,
            }
        ],
    }
